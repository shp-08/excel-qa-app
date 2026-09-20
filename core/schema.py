"""
schema.py - describe the loaded tables as text for the model.

Two levels of detail:

    table_index(store)              compact: names + columns only.
                                    Used to pick relevant tables when there are many.

    schema_context(store, names)    full: types, original headers, sample rows,
                                    join hints. Used to write SQL.

The token budget decides whether selection is needed at all. With a handful
of files the full context fits and we skip the selection step.
"""

from __future__ import annotations

import os

import pandas as pd

from core.ingest import DataStore, TableInfo

SCHEMA_TOKEN_BUDGET = int(os.getenv("SCHEMA_TOKEN_BUDGET", "6000"))
MAX_SAMPLE_COLS = 40
MAX_CELL_CHARS = 40


def estimate_tokens(text: str) -> int:
    """Rough but good enough: ~4 characters per token for English and SQL."""
    return len(text) // 4


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return "NULL"
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d") if v.hour == 0 and v.minute == 0 else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, float):
        return f"{v:g}"
    s = str(v).replace("\n", " ")
    return s if len(s) <= MAX_CELL_CHARS else s[: MAX_CELL_CHARS - 1] + "…"


def table_index(store: DataStore) -> str:
    """One line per table: enough to decide relevance, cheap to send."""
    lines = []
    for t in store.tables.values():
        lines.append(
            f"- {t.name}  (file '{t.file}', sheet '{t.sheet}', {t.row_count} rows): "
            + ", ".join(t.columns)
        )
    return "\n".join(lines)


def table_detail(t: TableInfo) -> str:
    """Full description of one table, including sample rows."""
    col_lines = []
    for c, ty in zip(t.columns, t.types):
        orig = t.original_columns.get(c, c)
        note = f'   -- original header "{orig}"' if orig.strip().lower().replace(" ", "_") != c else ""
        col_lines.append(f"    {c} {ty}{note}")

    cols = t.columns[:MAX_SAMPLE_COLS]
    sample_lines = [
        " | ".join(_fmt(v) for v in row)
        for row in t.sample[cols].itertuples(index=False)
    ]
    more = f"  (sample shows first {MAX_SAMPLE_COLS} of {len(t.columns)} columns)" if len(t.columns) > MAX_SAMPLE_COLS else ""

    return (
        f"TABLE {t.name}   -- from file '{t.file}', sheet '{t.sheet}', {t.row_count} rows\n"
        + "\n".join(col_lines)
        + f"\n  sample rows ({' | '.join(cols)}):{more}\n    "
        + "\n    ".join(sample_lines)
    )


def join_hints_text(store: DataStore, names: set[str]) -> str:
    rel = [h for h in store.join_hints if h.left_table in names and h.right_table in names]
    if not rel:
        return ""
    lines = [
        f"- {h.left_table}.{h.left_col} = {h.right_table}.{h.right_col}   (value overlap {int(h.overlap * 100)}%)"
        for h in rel
    ]
    return "LIKELY JOINS (detected from shared values, verify against the question):\n" + "\n".join(lines)


def schema_context(store: DataStore, names: list[str] | None = None) -> str:
    """Everything the SQL-writing prompt needs for the given tables (default: all)."""
    names = names or list(store.tables)
    parts = [table_detail(store.tables[n]) for n in names if n in store.tables]
    hints = join_hints_text(store, set(names))
    if hints:
        parts.append(hints)
    return "\n\n".join(parts)


def needs_selection(store: DataStore) -> bool:
    """True when the full schema is too big to send for every question."""
    return estimate_tokens(schema_context(store)) > SCHEMA_TOKEN_BUDGET
