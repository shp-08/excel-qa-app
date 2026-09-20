"""
query.py - question in, checked SQL result out.

The pipeline for one question:

    1. select tables      only when the schema is over the token budget
    2. write SQL          model sees schema + samples + join hints + recent turns
    3. check SQL          exactly one statement, SELECT only, DuckDB must be able to
                          plan it (catches invented tables / columns), and every
                          join must match at least one row (catches invented links)
    4. run SQL            with a timeout and a row cap
    5. retry              any error from 3 or 4 goes back to the model (max 2 times);
                          an empty result gets one retry with a hint
    6. narrate            a second call turns the RESULT ROWS into a sentence,
                          so numbers come from DuckDB, never from the model

The model has two other ways to reply instead of SQL:
    CANNOT_ANSWER: <reason>   the data cannot answer this, shown instead of a made-up query
    CHAT: <reply>             greetings and "what can I ask" need conversation, not a query

Nothing here imports Streamlit, so it can be tested from a plain script.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import duckdb
import pandas as pd

from core import llm, log
from core.ingest import DataStore
from core.schema import needs_selection, schema_context, table_index

MAX_RETRIES = 2            # corrections after a failed query
MAX_SELECTED_TABLES = 6
MAX_RESULT_ROWS = 5000
QUERY_TIMEOUT_SECONDS = 10
HISTORY_TURNS = 3          # how many earlier question+SQL pairs the model sees
NARRATE_ROWS = 20          # result rows shown to the model for the summary

REFUSAL_MARKER = "CANNOT_ANSWER:"
CHAT_MARKER = "CHAT:"


# ----------------------------------------------------------------------------
# Result objects
# ----------------------------------------------------------------------------

@dataclass
class Attempt:
    """One try at writing SQL. Kept for the trace shown under each answer."""
    sql: str
    error: str | None = None


@dataclass
class QueryResult:
    status: str                          # "ok" | "chat" | "refused" | "failed"
    question: str
    sql: str = ""
    df: pd.DataFrame | None = None
    truncated: bool = False              # True if the row cap cut the result
    message: str = ""                    # chat reply, refusal reason, or final error
    tables_used: list[str] = field(default_factory=list)
    selection_ran: bool = False
    selected_tables: list[str] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    seconds: float = 0.0


# ----------------------------------------------------------------------------
# Step 1: table selection (only for large schemas)
# ----------------------------------------------------------------------------

SELECT_SYSTEM = f"""You pick which database tables are needed to answer a question.
You get a list of tables with their columns, and a question.
Reply with the table names only, comma separated, nothing else.
Two exceptions:
- The message is a greeting, thanks, or asks what data is loaded or what can be
  asked. Reply with:
  {CHAT_MARKER} <a short friendly reply. For a greeting or "what can I ask", say in
  plain words what data is loaded and suggest 3 example questions as a bullet list.>
- The message asks for something that none of these tables could contain. Reply with:
  {REFUSAL_MARKER} <one sentence explaining what is missing>
Include every table the query must read, including a table needed only to join
two others. Do not add tables just in case: most questions need 1 to 3.
Maximum {MAX_SELECTED_TABLES} tables."""


def select_tables(store: DataStore, question: str, recent_tables: list[str], ask: Callable,
                  previous_question: str = "") -> tuple[list[str], str, str]:
    """Returns (table names, status, message). status is "chat" or "refused" when no SQL is needed."""
    user = f"TABLES:\n{table_index(store)}\n\n"
    if recent_tables:
        # without this, a follow-up like "now only the top 3" looks meaningless to this step
        user += (
            f'The previous question was: "{previous_question}"\n'
            f"It used these tables: {', '.join(recent_tables)}\n"
            'If the new message is a follow-up to it ("now only the top 3", "by month instead"), '
            "reply with those same tables.\n\n"
        )
    user += f"QUESTION: {question}"

    reply = ask([{"role": "system", "content": SELECT_SYSTEM}, {"role": "user", "content": user}])
    if reply.lstrip().startswith(CHAT_MARKER):
        return [], "chat", reply.split(CHAT_MARKER, 1)[1].strip()
    if reply.lstrip().startswith(REFUSAL_MARKER):
        return [], "refused", reply.split(REFUSAL_MARKER, 1)[1].strip()
    picked = [n for n in store.tables if re.search(rf"\b{re.escape(n)}\b", reply)]
    if not picked:                       # model replied with nothing usable
        picked = recent_tables or list(store.tables)
    return picked[:MAX_SELECTED_TABLES], "", ""


# ----------------------------------------------------------------------------
# Step 2: SQL generation
# ----------------------------------------------------------------------------

SQL_SYSTEM = f"""You write DuckDB SQL to answer questions about tables loaded from Excel files.

You reply in exactly one of three ways.

1. The message is a greeting, thanks, or asks what data is loaded or what can be
   asked. Do NOT write SQL. Reply with:
   {CHAT_MARKER} <a short friendly reply. For a greeting or "what can I ask", say in
   plain words what data is loaded and suggest 3 example questions as a bullet list.
   Otherwise just answer briefly, without suggestions.>

2. The message is a data question but the data cannot answer it (the needed
   column or table does not exist, or it is not about this data). Reply with:
   {REFUSAL_MARKER} <one sentence explaining what is missing>

3. Otherwise, write SQL.

Rules for SQL:
- Reply with ONE SELECT statement inside a ```sql code block, and nothing else.
- Use only the tables and columns listed in the schema. Never invent names.
- Text values in filters must match the sample rows exactly (case, spelling).
  For user-typed text prefer case-insensitive matching with ILIKE or lower().
- To combine tables, use the LIKELY JOINS section when it fits the question.
- Never join two tables on id columns that mean different things just because both
  are ids. If the tables the question needs have no real link, use reply type 2
  and say which link is missing.
- Give computed columns readable aliases, e.g. SUM(amount) AS total_amount.
- Round decimal results to 2 places.
- For "top" or "highest" questions use ORDER BY with LIMIT.
- If the question is a follow-up ("now by month", "only for 2024"), modify the
  previous SQL shown in the conversation."""


def build_sql_messages(schema_text: str, question: str, history: list[dict]) -> list[dict]:
    """history items are {"question": ..., "sql": ...} from earlier successful answers."""
    messages = [{"role": "system", "content": SQL_SYSTEM + "\n\nSCHEMA:\n" + schema_text}]
    for turn in history[-HISTORY_TURNS:]:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": f"```sql\n{turn['sql']}\n```"})
    messages.append({"role": "user", "content": question})
    return messages


_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)


def extract_sql(reply: str) -> str:
    """Take the SQL out of a code fence if there is one, and drop a trailing ';'."""
    m = _FENCE.search(reply)
    sql = m.group(1) if m else reply
    return sql.strip().rstrip(";").strip()


# ----------------------------------------------------------------------------
# Step 3: checks before running
# ----------------------------------------------------------------------------

def check_sql(store: DataStore, sql: str) -> str | None:
    """
    Returns an error message for the model, or None if the SQL is safe to run.

    We let DuckDB itself parse the text, which is more reliable than regex:
    it tells us how many statements there are and what type each one is.
    Then EXPLAIN plans the query without running it, so an invented column
    fails here with a precise message ("column X not found, candidates: ...").
    """
    if not sql:
        return "The reply contained no SQL."
    try:
        statements = store.con.extract_statements(sql)
    except duckdb.Error as e:
        return f"SQL syntax error: {e}"
    if len(statements) != 1:
        return "Write exactly one SQL statement."
    if statements[0].type != duckdb.StatementType.SELECT:
        return "Only SELECT statements are allowed."
    try:
        store.con.execute(f"EXPLAIN {sql}")
    except duckdb.Error as e:
        return str(e)
    return check_joins(store, sql)


_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+\"?(\w+)\"?(?:\s+(?:AS\s+)?(\w+))?", re.I)
_SIDE = r"(?:(?:TRY_)?CAST\s*\(\s*)?\"?(\w+)\"?\.\"?(\w+)\"?(?:\s+AS\s+\w+\s*\))?"
_JOIN_EQUALITY = re.compile(_SIDE + r"\s*=\s*" + _SIDE, re.I)
_NOT_AN_ALIAS = {"on", "where", "join", "left", "right", "inner", "outer", "full", "cross",
                 "group", "order", "limit", "using", "union", "having", "natural"}


def check_joins(store: DataStore, sql: str) -> str | None:
    """
    A join between two columns that share no values is a join the model made up.

    It is the most dangerous kind of mistake because nothing fails: asked for
    "orders per employee" when orders do not record an employee, a model will
    happily write  employees.id = orders.customer_id  and the result is a tidy
    table of zeros. So for every  a.x = b.y  between two loaded tables we ask
    DuckDB whether any value actually matches, and send the model back if not.

    Best effort: anything this simple parser does not understand is skipped.
    """
    aliases = {}
    for table, alias in _TABLE_REF.findall(sql):
        if table.lower() in store.tables:
            aliases[table.lower()] = table.lower()
            if alias and alias.lower() not in _NOT_AN_ALIAS:
                aliases[alias.lower()] = table.lower()

    for a, col_a, b, col_b in _JOIN_EQUALITY.findall(sql):
        table_a, table_b = aliases.get(a.lower()), aliases.get(b.lower())
        if not table_a or not table_b or table_a == table_b:
            continue
        if col_a.lower() not in store.tables[table_a].columns or col_b.lower() not in store.tables[table_b].columns:
            continue
        # compare as-is first (12.0 equals 12); if the types cannot be compared, compare as text
        probes = [f'x."{col_a}" = y."{col_b}"', f'CAST(x."{col_a}" AS VARCHAR) = CAST(y."{col_b}" AS VARCHAR)']
        matched = "unknown"
        for condition in probes:
            try:
                matched = store.con.execute(
                    f'SELECT 1 FROM "{table_a}" x JOIN "{table_b}" y ON {condition} LIMIT 1').fetchone()
                break
            except duckdb.Error:
                continue
        if matched is None:
            return (
                f"The join {table_a}.{col_a} = {table_b}.{col_b} matches no rows at all: these two columns "
                "do not hold the same kind of value, so they are not related. Do not join unrelated id columns. "
                "Join only through the LIKELY JOINS or through columns that clearly mean the same thing. "
                f"If the data has no real link between the tables this question needs, reply with "
                f"{REFUSAL_MARKER} and say which link is missing."
            )
    return None


# ----------------------------------------------------------------------------
# Step 4: run with guards
# ----------------------------------------------------------------------------

def run_sql(store: DataStore, sql: str) -> tuple[pd.DataFrame, bool]:
    """Run the query. Returns (rows, truncated). Raises duckdb.Error on failure."""
    timer = threading.Timer(QUERY_TIMEOUT_SECONDS, store.con.interrupt)
    timer.start()
    try:
        cur = store.con.execute(sql)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_RESULT_ROWS + 1)      # one extra row tells us if there was more
    except duckdb.InterruptException:
        raise duckdb.Error(
            f"Query took longer than {QUERY_TIMEOUT_SECONDS}s and was stopped. "
            "Check the join conditions, a missing ON clause multiplies rows."
        )
    finally:
        timer.cancel()
    truncated = len(rows) > MAX_RESULT_ROWS
    return pd.DataFrame(rows[:MAX_RESULT_ROWS], columns=columns), truncated


def tables_in_sql(store: DataStore, sql: str) -> list[str]:
    return [n for n in store.tables if re.search(rf"\b{re.escape(n)}\b", sql, re.I)]


# ----------------------------------------------------------------------------
# Steps 1-5 together
# ----------------------------------------------------------------------------

def answer_question(
    store: DataStore,
    question: str,
    history: list[dict] | None = None,
    ask: Callable[[list[dict]], str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> QueryResult:
    """
    history:  earlier successful turns as {"question", "sql", "tables"}.
    ask:      the function that calls the model. Tests pass a fake one.
    progress: called with a short text at each step, so the UI can show what is happening.
    """
    ask = ask or llm.chat
    progress = progress or (lambda text: None)
    started = time.time()
    history = history or []
    log.info("QUESTION  %s", question)
    result = QueryResult(status="failed", question=question)

    if not store.tables:
        result.message = "Upload at least one Excel file first."
        return result

    # 1. narrow the schema if it is too big to send whole
    names = list(store.tables)
    if needs_selection(store):
        progress("Many tables loaded, picking the relevant ones…")
        recent = [t for t in (history[-1]["tables"] if history else []) if t in store.tables]
        select_ask = ask if ask is not llm.chat else llm.select_chat
        previous = history[-1]["question"] if history else ""
        names, early_status, early_message = select_tables(store, question, recent, select_ask, previous)
        result.selection_ran = True
        if early_status:                                  # greeting or off-topic: no SQL needed
            result.status, result.message = early_status, early_message
            result.seconds = round(time.time() - started, 2)
            log.info("DONE      %s (decided by the selection step), %ss", early_status, result.seconds)
            return result
        log.info("SELECTED  %s", ", ".join(names))
    result.selected_tables = names

    messages = build_sql_messages(schema_context(store, names), question, history)
    retries_left = MAX_RETRIES
    empty_retry_used = False

    progress("Reading your question…")
    while True:
        reply = ask(messages)

        # greeting or "what can I ask": no query needed
        if reply.lstrip().startswith(CHAT_MARKER):
            result.status = "chat"
            result.message = reply.split(CHAT_MARKER, 1)[1].strip()
            break

        # the model may decline instead of inventing a query
        if REFUSAL_MARKER in reply:
            result.status = "refused"
            result.message = reply.split(REFUSAL_MARKER, 1)[1].strip()
            break

        sql = extract_sql(reply)
        log.info("SQL try %d %s", len(result.attempts) + 1, " ".join(sql.split()))
        progress("Checking the query…")
        error = check_sql(store, sql)
        df, truncated = None, False
        if error is None:
            progress("Running the query on your data…")
            try:
                df, truncated = run_sql(store, sql)
            except duckdb.Error as e:
                error = str(e)

        # an empty result is usually a wrong literal in a WHERE clause: one more try
        if error is None and df.empty and not empty_retry_used:
            empty_retry_used = True
            log.info("EMPTY     0 rows, asking the model to double-check")
            progress("No rows came back, double-checking the filters…")
            result.attempts.append(Attempt(sql, "returned 0 rows"))
            messages += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content":
                    "That query returned 0 rows. Check the filter values against the sample rows "
                    "(spelling, case, date format) and the join columns. If the query is right and "
                    "the answer really is 'none', return the same query again."},
            ]
            continue

        result.attempts.append(Attempt(sql, error))
        if error:
            log.info("ERROR     %s", error.splitlines()[0])

        if error is None:
            result.status = "ok"
            result.sql, result.df, result.truncated = sql, df, truncated
            result.tables_used = tables_in_sql(store, sql)
            break

        if retries_left == 0:
            result.sql = sql
            result.message = f"I could not produce a working query. Last error: {error}"
            break

        retries_left -= 1
        progress(f"The query had an error, fixing it (retry {MAX_RETRIES - retries_left} of {MAX_RETRIES})…")
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"That query failed:\n{error}\n\nFix it. Reply with the corrected SQL only."},
        ]

    result.seconds = round(time.time() - started, 2)
    rows = len(result.df) if result.df is not None else 0
    log.info("DONE      %s, %d rows, %d attempt(s), %ss  %s",
             result.status, rows, len(result.attempts), result.seconds, result.message)
    return result


# ----------------------------------------------------------------------------
# Suggested questions (shown as clickable chips in the UI)
# ----------------------------------------------------------------------------

SUGGEST_SYSTEM = """You suggest questions a user could ask about the tables they uploaded.
You get a list of tables with their columns.
Write {n} short analytical questions (at most 15 words each) that this data can answer with a calculation
(totals, averages, counts, top items, trends over time).
If there is more than one table, at least one question must need two tables combined.
Use plain words a business user would use, not column or table names.
Reply with one question per line. No numbering, no bullets, nothing else."""


def suggest_questions(store: DataStore, n: int = 4, ask: Callable | None = None) -> list[str]:
    """One cheap call per upload. Returns [] on any problem: suggestions are a nicety."""
    if not store.tables:
        return []
    ask = ask or (lambda m: llm.select_chat(m, max_tokens=250, temperature=0.4))
    try:
        reply = ask([
            {"role": "system", "content": SUGGEST_SYSTEM.format(n=n)},
            {"role": "user", "content": "TABLES:\n" + table_index(store)},
        ])
    except Exception as e:
        log.info("SUGGEST   skipped: %s", str(e).splitlines()[0][:120])
        return []
    lines = [re.sub(r"^[\s\-\*\d\.\)]+", "", line).strip() for line in reply.splitlines()]
    questions = [q for q in lines if q.endswith("?") and len(q) <= 150]
    log.info("SUGGEST   %d question(s)", len(questions[:n]))
    return questions[:n]


# ----------------------------------------------------------------------------
# Step 6: narration, grounded in the result
# ----------------------------------------------------------------------------

NARRATE_SYSTEM = """You explain the result of a data query to a business user.
You get the question and the result table that a database computed for it.

Rules:
- Answer the question in 1 to 3 sentences using ONLY values from the result table.
- Copy numbers exactly as they appear. Do not recalculate, add up, or estimate.
- Do not mention SQL, queries, or tables. Do not repeat the whole table, it is shown below your answer.
- If the result is empty, say that nothing in the data matches."""


def narration_messages(result: QueryResult) -> list[dict]:
    df = result.df
    shown = df.head(NARRATE_ROWS)
    table_text = shown.to_csv(index=False)
    note = ""
    if len(df) > NARRATE_ROWS:
        more = f"{len(df)}+" if result.truncated else str(len(df))
        note = f"\n(first {NARRATE_ROWS} of {more} rows shown)"
    user = f"QUESTION: {result.question}\n\nRESULT TABLE (csv):\n{table_text}{note}"
    return [{"role": "system", "content": NARRATE_SYSTEM}, {"role": "user", "content": user}]


_NUMBER = re.compile(r"\d[\d,]*\.?\d*")


def ungrounded_numbers(text: str, df: pd.DataFrame) -> list[str]:
    """
    Numbers in the model's sentence that do not appear in the result.
    Small integers are skipped ("top 3", "2 regions"), as is the row count.
    An empty list means the sentence is consistent with the data.
    """
    allowed: set[float] = {float(len(df))}
    for value in df.head(NARRATE_ROWS).to_numpy().ravel():
        for token in _NUMBER.findall(str(value)):      # also covers years inside dates
            try:
                allowed.add(float(token.replace(",", "")))
            except ValueError:
                pass

    missing = []
    for token in _NUMBER.findall(text):
        try:
            n = float(token.replace(",", "").rstrip("."))
        except ValueError:
            continue
        if n <= 10 and n == int(n):
            continue
        if not any(abs(n - a) <= 0.005 + 1e-9 * abs(a) or round(a, 2) == n or round(a) == n for a in allowed):
            missing.append(token)
    return missing
