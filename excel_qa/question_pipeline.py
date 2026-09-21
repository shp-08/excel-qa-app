"""The main loop: question -> pick tables -> model writes SQL -> check -> run -> retry on error."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

import duckdb
import pandas as pd

from excel_qa import llm_client
from excel_qa.file_loader import DataStore
from excel_qa.logger import log
from excel_qa.prompts import (
    CANNOT_ANSWER_MARKER, CHAT_MARKER, EMPTY_RESULT_PROMPT, FIX_SQL_PROMPT,
    MAX_PICKED_TABLES, PICK_TABLES_PROMPT, WRITE_SQL_PROMPT,
)
from excel_qa.schema_prompt import build_schema_prompt, build_table_index, schema_is_too_large
from excel_qa.sql_checks import extract_sql, run_query, tables_used_in, validate_sql

MAX_SQL_RETRIES = 2
PREVIOUS_TURNS_SHOWN = 3               # earlier question + SQL pairs the model sees, for follow-ups


@dataclass
class SqlAttempt:
    """One try at writing SQL, kept for the trace shown under each answer."""
    sql: str
    error: str | None = None


@dataclass
class QuestionResult:
    status: str                        # "ok" | "chat" | "refused" | "failed"
    question: str
    sql: str = ""
    rows: pd.DataFrame | None = None
    rows_were_cut_off: bool = False
    message: str = ""                  # chat reply, refusal reason, or final error
    tables_used: list[str] = field(default_factory=list)
    tables_were_picked: bool = False   # True when the schema was too large to send whole
    picked_tables: list[str] = field(default_factory=list)
    attempts: list[SqlAttempt] = field(default_factory=list)
    seconds: float = 0.0


def _split_marker(reply: str, marker: str) -> str:
    return reply.split(marker, 1)[1].strip()


def pick_relevant_tables(store: DataStore, question: str, previous_turn: dict | None,
                         ask: Callable) -> tuple[list[str], str, str]:
    """Returns (table names, status, message). status is "chat" or "refused" when no SQL is needed."""
    prompt = f"TABLES:\n{build_table_index(store)}\n\n"
    previous_tables = [t for t in (previous_turn or {}).get("tables", []) if t in store.tables]
    if previous_tables:
        # without this, a follow-up like "now only the top 3" means nothing to this step
        prompt += (
            f'The previous question was: "{previous_turn["question"]}"\n'
            f"It used these tables: {', '.join(previous_tables)}\n"
            'If the new message is a follow-up to it ("now only the top 3", "by month instead"), '
            "reply with those same tables.\n\n"
        )
    prompt += f"QUESTION: {question}"

    reply = ask([{"role": "system", "content": PICK_TABLES_PROMPT}, {"role": "user", "content": prompt}])
    if reply.lstrip().startswith(CHAT_MARKER):
        return [], "chat", _split_marker(reply, CHAT_MARKER)
    if reply.lstrip().startswith(CANNOT_ANSWER_MARKER):
        return [], "refused", _split_marker(reply, CANNOT_ANSWER_MARKER)

    picked = [name for name in store.tables if re.search(rf"\b{re.escape(name)}\b", reply)]
    picked = picked or previous_tables or list(store.tables)        # fallback if the reply was unusable
    return picked[:MAX_PICKED_TABLES], "", ""


def build_sql_conversation(schema_text: str, question: str, previous_turns: list[dict]) -> list[dict]:
    """System prompt with the schema, then earlier question/SQL pairs, then the new question."""
    messages = [{"role": "system", "content": WRITE_SQL_PROMPT + "\n\nSCHEMA:\n" + schema_text}]
    for turn in previous_turns[-PREVIOUS_TURNS_SHOWN:]:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": f"```sql\n{turn['sql']}\n```"})
    messages.append({"role": "user", "content": question})
    return messages


def _validate_and_run(store: DataStore, sql: str, show_progress: Callable) -> tuple[pd.DataFrame | None, bool, str | None]:
    """Returns (rows, rows_were_cut_off, error)."""
    show_progress("Checking the query…")
    error = validate_sql(store, sql)
    if error:
        return None, False, error
    show_progress("Running the query on your data…")
    try:
        rows, cut_off = run_query(store, sql)
        return rows, cut_off, None
    except duckdb.Error as db_error:
        return None, False, str(db_error)


def answer_question(
    store: DataStore,
    question: str,
    previous_turns: list[dict] | None = None,
    ask: Callable[[list[dict]], str] | None = None,
    show_progress: Callable[[str], None] | None = None,
) -> QuestionResult:
    """previous_turns: earlier answers as {"question", "sql", "tables"}. ask: the model call (tests pass a fake)."""
    using_real_model = ask is None
    ask = ask or llm_client.ask_model
    show_progress = show_progress or (lambda text: None)
    previous_turns = previous_turns or []
    started = time.time()
    log.info("QUESTION  %s", question)
    result = QuestionResult(status="failed", question=question)

    def finish() -> QuestionResult:
        result.seconds = round(time.time() - started, 2)
        log.info("DONE      %s, %d rows, %d attempt(s), %ss  %s", result.status,
                 0 if result.rows is None else len(result.rows), len(result.attempts), result.seconds, result.message)
        return result

    if not store.tables:
        result.message = "Upload at least one Excel or CSV file first."
        return finish()

    # Step 1: with many tables, a cheap call first narrows down which ones to describe in full
    table_names = list(store.tables)
    if schema_is_too_large(store):
        show_progress("Many tables loaded, picking the relevant ones…")
        ask_picker = llm_client.ask_helper_model if using_real_model else ask
        table_names, early_status, early_message = pick_relevant_tables(
            store, question, previous_turns[-1] if previous_turns else None, ask_picker)
        result.tables_were_picked = True
        if early_status:                                         # a greeting or an off-topic question
            result.status, result.message = early_status, early_message
            return finish()
        log.info("PICKED    %s", ", ".join(table_names))
    result.picked_tables = table_names

    # Steps 2-5: write SQL, check it, run it, and send any error back for another try
    messages = build_sql_conversation(build_schema_prompt(store, table_names), question, previous_turns)
    retries_left = MAX_SQL_RETRIES
    already_retried_empty_result = False
    show_progress("Reading your question…")

    while True:
        reply = ask(messages)

        if reply.lstrip().startswith(CHAT_MARKER):
            result.status, result.message = "chat", _split_marker(reply, CHAT_MARKER)
            return finish()
        if CANNOT_ANSWER_MARKER in reply:
            result.status, result.message = "refused", _split_marker(reply, CANNOT_ANSWER_MARKER)
            return finish()

        sql = extract_sql(reply)
        log.info("SQL try %d %s", len(result.attempts) + 1, " ".join(sql.split()))
        rows, cut_off, error = _validate_and_run(store, sql, show_progress)

        # an empty result usually means a wrong value in a filter, so it gets one extra try
        if error is None and rows.empty and not already_retried_empty_result:
            already_retried_empty_result = True
            log.info("EMPTY     0 rows, asking the model to double-check")
            show_progress("No rows came back, double-checking the filters…")
            result.attempts.append(SqlAttempt(sql, "returned 0 rows"))
            messages += [{"role": "assistant", "content": reply}, {"role": "user", "content": EMPTY_RESULT_PROMPT}]
            continue

        result.attempts.append(SqlAttempt(sql, error))

        if error is None:
            result.status, result.sql = "ok", sql
            result.rows, result.rows_were_cut_off = rows, cut_off
            result.tables_used = tables_used_in(store, sql)
            return finish()

        log.info("ERROR     %s", error.splitlines()[0])
        if retries_left == 0:
            result.sql = sql
            result.message = f"I could not produce a working query. Last error: {error}"
            return finish()

        retries_left -= 1
        show_progress(f"The query had an error, fixing it (retry {MAX_SQL_RETRIES - retries_left} of {MAX_SQL_RETRIES})…")
        messages += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": FIX_SQL_PROMPT.format(error=error)}]
