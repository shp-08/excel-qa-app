"""Reads Excel and CSV files, cleans every sheet, and loads each one into DuckDB as a table."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from excel_qa.logger import log


@dataclass
class TableInfo:
    """What we know about one loaded sheet."""
    name: str                          # SQL table name: <file>_<sheet>
    file: str
    sheet: str
    columns: list[str]                 # cleaned column names
    types: list[str]                   # DuckDB types, same order as columns
    original_columns: dict[str, str]   # cleaned name -> header text in the Excel file
    row_count: int
    sample: pd.DataFrame               # first few rows, shown to the model


def _new_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    # model-written SQL runs here, so it must not be able to read files or URLs
    con.execute("SET enable_external_access=false")
    return con


@dataclass
class DataStore:
    """One per browser session: the DuckDB connection, the loaded tables and their links."""
    con: duckdb.DuckDBPyConnection = field(default_factory=_new_connection)
    tables: dict[str, TableInfo] = field(default_factory=dict)
    links: list = field(default_factory=list)          # list[TableLink], filled by link_detector


# --- Names ---

def to_sql_name(text: str, fallback: str = "col") -> str:
    """'Total Amount ($)' -> 'total_amount', safe to use unquoted in SQL."""
    text = re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return fallback
    return f"c_{text}" if text[0].isdigit() else text   # SQL names cannot start with a digit


def make_names_unique(names: list[str]) -> list[str]:
    """['id', 'name', 'id'] -> ['id', 'name', 'id_2']"""
    seen: dict[str, int] = {}
    unique = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        unique.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return unique


def build_table_name(file_name: str, sheet_name: str, is_only_sheet: bool) -> str:
    file_part = to_sql_name(re.sub(r"\.(xlsx?|csv)$", "", file_name, flags=re.I), "table")
    return file_part if is_only_sheet else f"{file_part}_{to_sql_name(sheet_name, 'sheet')}"


# --- Cleaning a sheet ---

def find_header_row(raw: pd.DataFrame, rows_to_scan: int = 10) -> int:
    """First row that is mostly filled and mostly text. Skips title rows and blank rows."""
    column_count = raw.shape[1]
    for i in range(min(rows_to_scan, len(raw))):
        row = raw.iloc[i]
        filled = row.notna().sum()
        if filled < max(2, 0.5 * column_count):
            continue
        text_cells = sum(isinstance(v, str) for v in row if pd.notna(v))
        if text_cells >= 0.6 * filled:
            return i
    return 0


_CURRENCY_AND_SEPARATORS = re.compile(r"[,\s$€£₹%]")
_LOOKS_LIKE_DATE = re.compile(r"^\s*\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")


_FIRST_TWO_NUMBERS = re.compile(r"^\s*(\d{1,4})[-/.](\d{1,2})")


def parse_date_column(values: pd.Series) -> pd.Series:
    """Parses a whole column with ONE day/month order, so 01/08/2024 is never read differently from 22/08/2024."""
    parts = values.dropna().astype(str).str.extract(_FIRST_TWO_NUMBERS).dropna()
    first, second = parts[0].astype(int), parts[1].astype(int)
    if (parts[0].str.len() == 4).all():                          # 2024-08-01: year first, no ambiguity
        return pd.to_datetime(values, errors="coerce", format="mixed", yearfirst=True)
    # a first number above 12 can only be a day; if no value settles it, fall back to month first
    day_first = bool((first > 12).any()) and not bool((second > 12).any())
    return pd.to_datetime(values, errors="coerce", format="mixed", dayfirst=day_first)


def convert_text_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Turns text like '$1,200.50' or '17/02/2024' into real numbers and dates."""
    for col in df.columns:
        column = df[col]
        if not (column.dtype == object or pd.api.types.is_string_dtype(column)):
            continue
        values = column.dropna().astype(str)
        if values.empty:
            continue

        # convert only when at least 90% of values parse, so real text columns are left alone
        as_number = pd.to_numeric(values.str.replace(_CURRENCY_AND_SEPARATORS, "", regex=True), errors="coerce")
        if as_number.notna().mean() >= 0.9:
            df[col] = pd.to_numeric(
                column.astype(str).str.replace(_CURRENCY_AND_SEPARATORS, "", regex=True), errors="coerce")
            continue

        if values.str.match(_LOOKS_LIKE_DATE).mean() >= 0.9:
            if parse_date_column(values).notna().mean() >= 0.9:
                df[col] = parse_date_column(column)
    return df


def clean_sheet(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]] | None:
    """Raw sheet -> tidy DataFrame plus {cleaned column: original header}. None if the sheet is empty."""
    raw = raw.dropna(how="all").dropna(axis=1, how="all")
    if raw.empty:
        return None

    header_row = find_header_row(raw)
    headers = raw.iloc[header_row].tolist()
    df = raw.iloc[header_row + 1:].reset_index(drop=True)
    if df.empty:
        return None

    # drop columns that have a header but no data (spacer columns)
    keep = [i for i in range(df.shape[1]) if df.iloc[:, i].notna().any()]
    df = df.iloc[:, keep]
    originals = [str(headers[i]).strip() if pd.notna(headers[i]) else "" for i in keep]

    cleaned = make_names_unique([to_sql_name(o, fallback=f"col_{i + 1}") for i, o in enumerate(originals)])
    df.columns = cleaned
    original_by_cleaned = {c: (o or c) for c, o in zip(cleaned, originals)}

    # rows below the header are all "object" type until pandas is asked to infer real types
    return convert_text_columns(df.infer_objects()), original_by_cleaned


# --- Loading into DuckDB ---

def read_raw_sheets(file_name: str, data: bytes) -> dict[str, pd.DataFrame]:
    """Every sheet as raw cells with no header assumed. A CSV counts as a file with one sheet."""
    if not file_name.lower().endswith(".csv"):
        return pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)
    # sep=None lets pandas detect commas, semicolons or tabs; latin-1 is the fallback for old exports
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return {"data": pd.read_csv(io.BytesIO(data), header=None, dtype=object, sep=None,
                                        engine="python", encoding=encoding)}
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not read the CSV file: unknown text encoding.")


def load_data_file(store: DataStore, file_name: str, data: bytes, sample_rows: int = 3) -> list[str]:
    """Loads every non-empty sheet of one Excel or CSV file. Returns the table names created."""
    sheets = read_raw_sheets(file_name, data)
    created = []
    for sheet_name, raw in sheets.items():
        cleaned = clean_sheet(raw)
        if cleaned is None:
            continue
        df, original_by_cleaned = cleaned

        name = build_table_name(file_name, sheet_name, is_only_sheet=len(sheets) == 1)
        if name in store.tables:                     # e.g. the same file uploaded twice
            name = make_names_unique(list(store.tables) + [name])[-1]

        store.con.register("_incoming", df)
        store.con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _incoming')
        store.con.unregister("_incoming")

        described = store.con.execute(f'DESCRIBE "{name}"').fetchall()
        store.tables[name] = TableInfo(
            name=name, file=file_name, sheet=sheet_name,
            columns=[row[0] for row in described], types=[row[1] for row in described],
            original_columns=original_by_cleaned, row_count=len(df), sample=df.head(sample_rows),
        )
        created.append(name)
        log.info("LOADED    %s  <- %s [%s]  %d rows x %d cols", name, file_name, sheet_name, len(df), df.shape[1])
    return created


def remove_data_file(store: DataStore, file_name: str) -> None:
    for name in [t.name for t in store.tables.values() if t.file == file_name]:
        store.con.execute(f'DROP TABLE IF EXISTS "{name}"')
        del store.tables[name]
