"""Checks model-written SQL before it runs, then runs it with a timeout and a row limit."""

from __future__ import annotations

import re
import threading

import duckdb
import pandas as pd

from excel_qa.file_loader import DataStore
from excel_qa.prompts import UNRELATED_JOIN_ERROR

MAX_RESULT_ROWS = 5000
QUERY_TIMEOUT_SECONDS = 10

_CODE_BLOCK = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)


def extract_sql(reply: str) -> str:
    """Takes the SQL out of a ```sql block if there is one, and drops a trailing ';'."""
    match = _CODE_BLOCK.search(reply)
    return (match.group(1) if match else reply).strip().rstrip(";").strip()


def validate_sql(store: DataStore, sql: str) -> str | None:
    """Returns an error message to send back to the model, or None if the SQL is safe to run."""
    if not sql:
        return "The reply contained no SQL."
    # DuckDB's own parser tells us how many statements there are and their type (no regex guessing)
    try:
        statements = store.con.extract_statements(sql)
    except duckdb.Error as error:
        return f"SQL syntax error: {error}"
    if len(statements) != 1:
        return "Write exactly one SQL statement."
    if statements[0].type != duckdb.StatementType.SELECT:
        return "Only SELECT statements are allowed."
    # EXPLAIN plans the query without running it, so an invented column fails here with a clear message
    try:
        store.con.execute(f"EXPLAIN {sql}")
    except duckdb.Error as error:
        return str(error)
    return find_unrelated_join(store, sql)


_TABLE_AND_ALIAS = re.compile(r"\b(?:FROM|JOIN)\s+\"?(\w+)\"?(?:\s+(?:AS\s+)?(\w+))?", re.I)
_COLUMN_REF = r"(?:(?:TRY_)?CAST\s*\(\s*)?\"?(\w+)\"?\.\"?(\w+)\"?(?:\s+AS\s+\w+\s*\))?"
_JOIN_CONDITION = re.compile(_COLUMN_REF + r"\s*=\s*" + _COLUMN_REF, re.I)
_SQL_KEYWORDS = {"on", "where", "join", "left", "right", "inner", "outer", "full", "cross",
                 "group", "order", "limit", "using", "union", "having", "natural"}


def find_unrelated_join(store: DataStore, sql: str) -> str | None:
    """Rejects a join whose two columns share no values: nothing fails, but the answer is all zeros."""
    table_by_alias = {}
    for table, alias in _TABLE_AND_ALIAS.findall(sql):
        if table.lower() in store.tables:
            table_by_alias[table.lower()] = table.lower()
            if alias and alias.lower() not in _SQL_KEYWORDS:
                table_by_alias[alias.lower()] = table.lower()

    for left_alias, left_column, right_alias, right_column in _JOIN_CONDITION.findall(sql):
        left_table = table_by_alias.get(left_alias.lower())
        right_table = table_by_alias.get(right_alias.lower())
        if not left_table or not right_table or left_table == right_table:
            continue
        if (left_column.lower() not in store.tables[left_table].columns
                or right_column.lower() not in store.tables[right_table].columns):
            continue

        # compare as-is first (12.0 equals 12), and as text if the types cannot be compared
        conditions = [f'x."{left_column}" = y."{right_column}"',
                      f'CAST(x."{left_column}" AS VARCHAR) = CAST(y."{right_column}" AS VARCHAR)']
        first_match = "not checked"
        for condition in conditions:
            try:
                first_match = store.con.execute(
                    f'SELECT 1 FROM "{left_table}" x JOIN "{right_table}" y ON {condition} LIMIT 1').fetchone()
                break
            except duckdb.Error:
                continue
        if first_match is None:
            return UNRELATED_JOIN_ERROR.format(left=f"{left_table}.{left_column}", right=f"{right_table}.{right_column}")
    return None


def run_query(store: DataStore, sql: str) -> tuple[pd.DataFrame, bool]:
    """Runs the query. Returns (rows, was_cut_off). Raises duckdb.Error on failure or timeout."""
    timer = threading.Timer(QUERY_TIMEOUT_SECONDS, store.con.interrupt)
    timer.start()
    try:
        cursor = store.con.execute(sql)
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)             # one extra row tells us there was more
    except duckdb.InterruptException:
        raise duckdb.Error(f"Query took longer than {QUERY_TIMEOUT_SECONDS}s and was stopped. "
                           "Check the join conditions, a missing ON clause multiplies rows.")
    finally:
        timer.cancel()
    result = pd.DataFrame(rows[:MAX_RESULT_ROWS], columns=columns)
    # 1373022.7800000005 -> 1373022.78: removes floating-point noise without losing real precision
    return result.round(6), len(rows) > MAX_RESULT_ROWS


def tables_used_in(store: DataStore, sql: str) -> list[str]:
    return [name for name in store.tables if re.search(rf"\b{re.escape(name)}\b", sql, re.I)]
