"""Describes the loaded tables as text for the model: a short index, or full detail with sample rows."""

from __future__ import annotations

import os

import pandas as pd

from excel_qa.file_loader import DataStore, TableInfo

SCHEMA_TOKEN_BUDGET = int(os.getenv("SCHEMA_TOKEN_BUDGET", "6000"))
MAX_SAMPLE_COLUMNS = 40
MAX_CELL_CHARS = 40


def estimate_tokens(text: str) -> int:
    """Roughly 4 characters per token for English and SQL."""
    return len(text) // 4


def _format_cell(value) -> str:
    if value is None or value is pd.NaT or (isinstance(value, float) and pd.isna(value)):
        return "NULL"
    if isinstance(value, pd.Timestamp):
        has_time = value.hour or value.minute
        return value.strftime("%Y-%m-%d %H:%M" if has_time else "%Y-%m-%d")
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value).replace("\n", " ")
    return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 1] + "…"


def build_table_index(store: DataStore) -> str:
    """One line per table (name and column names). Cheap enough to send when there are many tables."""
    return "\n".join(
        f"- {t.name}  (file '{t.file}', sheet '{t.sheet}', {t.row_count} rows): " + ", ".join(t.columns)
        for t in store.tables.values()
    )


def describe_table(table: TableInfo) -> str:
    """Full description of one table: columns, types, original headers and sample rows."""
    column_lines = []
    for column, column_type in zip(table.columns, table.types):
        original = table.original_columns.get(column, column)
        # the original header helps when the user words a question the way the spreadsheet does
        was_renamed = original.strip().lower().replace(" ", "_") != column
        column_lines.append(f"    {column} {column_type}" + (f'   -- original header "{original}"' if was_renamed else ""))

    shown = table.columns[:MAX_SAMPLE_COLUMNS]
    sample_lines = [" | ".join(_format_cell(v) for v in row) for row in table.sample[shown].itertuples(index=False)]
    cut_note = (f"  (sample shows first {MAX_SAMPLE_COLUMNS} of {len(table.columns)} columns)"
                if len(table.columns) > MAX_SAMPLE_COLUMNS else "")

    return (
        f"TABLE {table.name}   -- from file '{table.file}', sheet '{table.sheet}', {table.row_count} rows\n"
        + "\n".join(column_lines)
        + f"\n  sample rows ({' | '.join(shown)}):{cut_note}\n    "
        + "\n    ".join(sample_lines)
    )


def describe_links(store: DataStore, table_names: set[str]) -> str:
    links = [l for l in store.links if l.from_table in table_names and l.to_table in table_names]
    if not links:
        return ""
    return "LIKELY JOINS (detected from the data, verify against the question):\n" + "\n".join(
        f"- {l.from_table}.{l.from_column} = {l.to_table}.{l.to_column}   ({int(l.match_share * 100)}% of values match)"
        for l in links
    )


def build_schema_prompt(store: DataStore, table_names: list[str] | None = None) -> str:
    """Everything the SQL-writing prompt needs about the given tables (default: all of them)."""
    table_names = table_names or list(store.tables)
    parts = [describe_table(store.tables[name]) for name in table_names if name in store.tables]
    links = describe_links(store, set(table_names))
    return "\n\n".join(parts + ([links] if links else []))


def schema_is_too_large(store: DataStore) -> bool:
    """True when the full schema should not be sent with every question."""
    return estimate_tokens(build_schema_prompt(store)) > SCHEMA_TOKEN_BUDGET
