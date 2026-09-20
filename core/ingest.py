"""
ingest.py - turn uploaded Excel files into DuckDB tables.

Flow for each file:
    read every sheet with pandas
    -> find the real header row (skip title rows / blank rows)
    -> clean column names and coerce types (numbers-as-text, dates-as-text)
    -> copy into DuckDB as a table named  <file>_<sheet>

After all files are loaded, compute_join_hints() looks for columns in
different tables that share values, so the model can be told
"orders.customer_id probably joins customers.id".
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from core import log


# ----------------------------------------------------------------------------
# Data structures
# ----------------------------------------------------------------------------

@dataclass
class TableInfo:
    """Everything we know about one loaded sheet."""
    name: str                    # SQL table name, e.g. orders_2024_orders
    file: str                    # original file name
    sheet: str                   # original sheet name
    columns: list[str]           # clean SQL column names
    types: list[str]             # DuckDB types, same order as columns
    original_columns: dict[str, str]   # clean name -> original header text
    row_count: int
    sample: pd.DataFrame         # first few rows, for the prompt


@dataclass
class JoinHint:
    left_table: str
    left_col: str
    right_table: str
    right_col: str
    overlap: float               # 0..1, share of values that appear in both


def _new_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    # model-written SQL runs on this connection, so it must not be able to
    # read files or URLs (e.g. SELECT * FROM read_csv('C:/secrets.csv'))
    con.execute("SET enable_external_access=false")
    return con


@dataclass
class DataStore:
    """One per user session: a DuckDB connection plus metadata."""
    con: duckdb.DuckDBPyConnection = field(default_factory=_new_connection)
    tables: dict[str, TableInfo] = field(default_factory=dict)
    join_hints: list[JoinHint] = field(default_factory=list)


# ----------------------------------------------------------------------------
# Name cleaning
# ----------------------------------------------------------------------------

def clean_name(text: str, fallback: str = "col") -> str:
    """'Total Sales ($)' -> 'total_sales'. Safe to use unquoted in SQL."""
    text = str(text).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)      # anything odd -> underscore
    text = re.sub(r"_+", "_", text).strip("_")   # collapse and trim
    if not text:
        return fallback
    if text[0].isdigit():                        # SQL names must not start with a digit
        text = f"c_{text}"
    return text


def make_unique(names: list[str]) -> list[str]:
    """['id', 'name', 'id'] -> ['id', 'name', 'id_2']"""
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 1
            out.append(n)
    return out


# ----------------------------------------------------------------------------
# Header detection and cleaning
# ----------------------------------------------------------------------------

def detect_header_row(raw: pd.DataFrame, max_scan: int = 10) -> int:
    """
    Sheets often start with a title row or blank rows. We read with header=None
    and pick the first row that looks like a header: mostly non-empty and
    mostly text. Falls back to row 0.
    """
    ncols = raw.shape[1]
    for i in range(min(max_scan, len(raw))):
        row = raw.iloc[i]
        non_null = row.notna().sum()
        if non_null < max(2, 0.5 * ncols):
            continue
        texty = sum(isinstance(v, str) for v in row if pd.notna(v))
        if texty >= 0.6 * non_null:
            return i
    return 0


def _is_text_column(s: pd.Series) -> bool:
    return s.dtype == object or pd.api.types.is_string_dtype(s)


_NUMERIC_JUNK = re.compile(r"[,\s$€£₹%]")
_DATE_SHAPE = re.compile(r"^\s*\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")


def coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """
    Excel frequently stores numbers and dates as text ('$1,200.50', '2024-01-05').
    For each text column, try numeric then date; convert only if >= 90% of the
    non-null values parse, so we never mangle a genuine text column.
    """
    for col in df.columns:
        s = df[col]
        if not _is_text_column(s):
            continue
        non_null = s.dropna().astype(str)
        if len(non_null) == 0:
            continue

        # numeric attempt
        stripped = non_null.str.replace(_NUMERIC_JUNK, "", regex=True)
        as_num = pd.to_numeric(stripped, errors="coerce")
        if as_num.notna().mean() >= 0.9:
            df[col] = pd.to_numeric(
                s.astype(str).str.replace(_NUMERIC_JUNK, "", regex=True), errors="coerce"
            )
            continue

        # date attempt (only if values are shaped like dates)
        if non_null.str.match(_DATE_SHAPE).mean() >= 0.9:
            as_date = pd.to_datetime(non_null, errors="coerce", format="mixed")
            if as_date.notna().mean() >= 0.9:
                df[col] = pd.to_datetime(s, errors="coerce", format="mixed")
    return df


def clean_sheet(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]] | None:
    """Raw sheet (header=None) -> tidy DataFrame + {clean_col: original_header}."""
    raw = raw.dropna(how="all").dropna(axis=1, how="all")
    if raw.empty or raw.shape[1] == 0:
        return None

    h = detect_header_row(raw)
    header = raw.iloc[h].tolist()
    df = raw.iloc[h + 1:].reset_index(drop=True)
    if df.empty:
        return None

    # drop columns that are empty below the header (a header cell of ' ' is
    # not null, so the earlier dropna could not catch these)
    keep = [i for i in range(df.shape[1]) if df.iloc[:, i].notna().any()]
    df = df.iloc[:, keep]
    header = [header[i] for i in keep]

    originals = [str(x).strip() if pd.notna(x) else "" for x in header]
    clean = make_unique([clean_name(o, fallback=f"col_{i + 1}") for i, o in enumerate(originals)])
    df.columns = clean
    mapping = {c: (o or c) for c, o in zip(clean, originals)}

    # after slicing below the header, pandas still thinks everything is object;
    # infer_objects turns numeric-looking object columns into real numbers
    df = df.infer_objects()
    df = coerce_types(df)
    return df, mapping


# ----------------------------------------------------------------------------
# Loading into DuckDB
# ----------------------------------------------------------------------------

def table_name_for(file_name: str, sheet_name: str, single_sheet: bool) -> str:
    stem = re.sub(r"\.xlsx?$", "", file_name, flags=re.I)
    if single_sheet:
        return clean_name(stem, "table")
    return f"{clean_name(stem, 'table')}_{clean_name(sheet_name, 'sheet')}"


def ingest_file(store: DataStore, file_name: str, data: bytes, sample_rows: int = 3) -> list[str]:
    """Load every non-empty sheet of one Excel file. Returns the table names created."""
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)
    created = []
    single = len(sheets) == 1
    for sheet_name, raw in sheets.items():
        cleaned = clean_sheet(raw)
        if cleaned is None:
            continue
        df, mapping = cleaned
        name = table_name_for(file_name, sheet_name, single)
        if name in store.tables:                     # same file uploaded twice, etc.
            name = make_unique(list(store.tables) + [name])[-1]

        # register the DataFrame as a temp view, copy into a real table, drop the view
        store.con.register("_incoming", df)
        store.con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _incoming')
        store.con.unregister("_incoming")

        desc = store.con.execute(f'DESCRIBE "{name}"').fetchall()
        store.tables[name] = TableInfo(
            name=name,
            file=file_name,
            sheet=sheet_name,
            columns=[r[0] for r in desc],
            types=[r[1] for r in desc],
            original_columns=mapping,
            row_count=len(df),
            sample=df.head(sample_rows),
        )
        created.append(name)
        log.info("LOADED    %s  <- %s [%s]  %d rows x %d cols", name, file_name, sheet_name, len(df), df.shape[1])
    return created


def remove_file(store: DataStore, file_name: str) -> None:
    for name in [t.name for t in store.tables.values() if t.file == file_name]:
        store.con.execute(f'DROP TABLE IF EXISTS "{name}"')
        del store.tables[name]


# ----------------------------------------------------------------------------
# Join hints
# ----------------------------------------------------------------------------
#
# A useful link goes from a foreign key to a primary key:
#
#     child_table.parent_id  ->  parent_table.parent_id
#
#   - the target (primary key) must be unique in its table
#   - the source's values must be contained in the target's values
#   - for integer ids the names must also agree, because surrogate keys are
#     all 1, 2, 3 ... so any id column would "match" any other by accident
#
# Comparing every column with every column (the first version of this code)
# produced 2,610 links on a real 22-sheet workbook. This version produced 77
# on the same file, all of them real relationships.

_KEY_WORDS = re.compile(r"(^|_)(id|key|code|no|num|number|sku|ref)($|_)")
_INT_TYPES = ("BIGINT", "INTEGER", "HUGEINT", "SMALLINT", "TINYINT")
MAX_KEY_VALUES = 100_000       # primary key values held in memory per column
MAX_SOURCE_VALUES = 1_000      # foreign key values sampled for the containment check


def _values(store: DataStore, table: str, col: str, limit: int) -> set[str]:
    rows = store.con.execute(
        f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT {limit}'
    ).fetchall()
    out = set()
    for (v,) in rows:
        if isinstance(v, float) and v.is_integer():     # ids with blanks load as 12.0
            v = int(v)
        out.add(str(v).strip().lower())
    return out


def _key_columns(store: DataStore, t: TableInfo) -> tuple[list[dict], list[dict]]:
    """
    Split a table's columns into possible primary keys and possible foreign keys.
    Each entry: {"col", "is_int", "position"}.
    """
    primary, foreign = [], []
    for position, (col, typ) in enumerate(zip(t.columns, t.types)):
        is_int = typ in _INT_TYPES
        is_text = typ == "VARCHAR"
        named = bool(_KEY_WORDS.search(col))
        # ids that contain blanks become DOUBLE in pandas; accept them only if named like a key
        if not (is_int or is_text or (typ == "DOUBLE" and named)):
            continue
        distinct, filled = store.con.execute(
            f'SELECT COUNT(DISTINCT "{col}"), COUNT("{col}") FROM "{t.name}"'
        ).fetchone()
        if distinct < 2:
            continue
        entry = {"col": col, "is_int": not is_text, "position": position}
        if distinct == t.row_count == filled:            # unique and no blanks
            primary.append(entry)
        if named or is_text:
            foreign.append(entry)
    return primary, foreign


def _names_agree(source_col: str, target_col: str) -> bool:
    """assigned_owner_id -> owner_id: the source name ends with the target name."""
    return source_col == target_col or source_col.endswith("_" + target_col)


def compute_join_hints(store: DataStore, min_overlap: float = 0.9, max_hints: int = 200) -> list[JoinHint]:
    keys = {name: _key_columns(store, t) for name, t in store.tables.items()}
    pk_values = {
        (name, pk["col"]): _values(store, name, pk["col"], MAX_KEY_VALUES)
        for name, (primary, _) in keys.items() for pk in primary
    }

    hints: list[JoinHint] = []
    for src_table, (_, foreign) in keys.items():
        for fk in foreign:
            src_values = None
            best = None                                   # (score, JoinHint)
            for dst_table, (primary, _) in keys.items():
                if dst_table == src_table:
                    continue
                for pk in primary:
                    if fk["is_int"] != pk["is_int"]:
                        continue
                    if fk["is_int"] and not _names_agree(fk["col"], pk["col"]):
                        continue
                    if src_values is None:
                        src_values = _values(store, src_table, fk["col"], MAX_SOURCE_VALUES)
                    if not src_values:
                        continue
                    overlap = len(src_values & pk_values[(dst_table, pk["col"])]) / len(src_values)
                    if overlap < min_overlap:
                        continue
                    # several tables may hold the same id uniquely; the real parent usually
                    # has it as its first column and the same name
                    score = (overlap, pk["position"] == 0, fk["col"] == pk["col"])
                    if best is None or score > best[0]:
                        best = (score, JoinHint(src_table, fk["col"], dst_table, pk["col"], round(overlap, 2)))
            if best:
                hints.append(best[1])

    hints.sort(key=lambda h: -h.overlap)
    store.join_hints = hints[:max_hints]
    log.info("JOINS     %d link(s) detected across %d table(s)", len(store.join_hints), len(store.tables))
    return store.join_hints
