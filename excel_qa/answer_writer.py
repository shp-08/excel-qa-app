"""Words the answer from the result rows, checks its numbers, and suggests questions for new data."""

from __future__ import annotations

import re
from typing import Callable

import pandas as pd

from excel_qa import llm_client
from excel_qa.file_loader import DataStore
from excel_qa.logger import log
from excel_qa.prompts import FIX_SUMMARY_PROMPT, SUGGEST_QUESTIONS_PROMPT, WRITE_SUMMARY_PROMPT
from excel_qa.question_pipeline import QuestionResult
from excel_qa.schema_prompt import build_schema_prompt, build_table_index, schema_is_too_large

ROWS_SHOWN_TO_MODEL = 20
_NUMBER = re.compile(r"\d[\d,]*\.?\d*")


def build_summary_conversation(result: QuestionResult) -> list[dict]:
    """The model words the answer from the computed rows, so it never has to do arithmetic."""
    rows = result.rows
    table_text = rows.head(ROWS_SHOWN_TO_MODEL).to_csv(index=False)
    if len(rows) > ROWS_SHOWN_TO_MODEL:
        total = f"{len(rows)}+" if result.rows_were_cut_off else str(len(rows))
        table_text += f"\n(first {ROWS_SHOWN_TO_MODEL} of {total} rows shown)"
    return [
        {"role": "system", "content": WRITE_SUMMARY_PROMPT},
        {"role": "user", "content": f"QUESTION: {result.question}\n\nRESULT TABLE (csv):\n{table_text}"},
    ]


def _to_number(token: str) -> float | None:
    try:
        return float(token.replace(",", "").rstrip("."))
    except ValueError:
        return None


def numbers_not_in_result(summary: str, rows: pd.DataFrame) -> list[str]:
    """Numbers in the summary that do not appear in the result. An empty list means it is consistent."""
    allowed = {float(len(rows))}
    for cell in rows.head(ROWS_SHOWN_TO_MODEL).to_numpy().ravel():
        allowed.update(n for n in map(_to_number, _NUMBER.findall(str(cell))) if n is not None)

    missing = []
    for token in _NUMBER.findall(summary):
        number = _to_number(token)
        # small whole numbers are skipped: "top 3" or "2 groups" are not claims about the data
        if number is None or (number <= 10 and number == int(number)):
            continue
        if not any(abs(number - a) <= 0.005 or round(a, 2) == number or round(a) == number for a in allowed):
            missing.append(token)
    return missing


def rewrite_summary(result: QuestionResult, summary: str, wrong_numbers: list[str],
                    ask: Callable | None = None) -> tuple[str, list[str]]:
    """One retry when the summary misquoted a number. Returns (summary, numbers still wrong)."""
    ask = ask or (lambda messages: llm_client.ask_model(messages, max_tokens=300))
    messages = build_summary_conversation(result) + [
        {"role": "assistant", "content": summary},
        {"role": "user", "content": FIX_SUMMARY_PROMPT.format(numbers=", ".join(wrong_numbers))},
    ]
    try:
        rewritten = ask(messages)
    except Exception as error:
        log.info("SUMMARY   rewrite skipped: %s", str(error).splitlines()[0][:100])
        return summary, wrong_numbers
    still_wrong = numbers_not_in_result(rewritten, result.rows)
    log.info("SUMMARY   rewritten after misquoting %s -> %s", wrong_numbers, "ok" if not still_wrong else still_wrong)
    # if even the rewrite is wrong, a plain sentence beats a wrong one: the table below has the facts
    return (rewritten, []) if not still_wrong else ("Here is the result, computed from your data.", [])


def suggest_questions(store: DataStore, count: int = 4, ask: Callable | None = None) -> list[str]:
    """One cheap call per upload. Returns [] on any problem, because suggestions are optional."""
    if not store.tables:
        return []
    ask = ask or (lambda messages: llm_client.ask_helper_model(messages, max_tokens=250, temperature=0.4))
    # a small schema is sent with its sample rows, so suggestions only mention values that exist
    tables_text = build_table_index(store) if schema_is_too_large(store) else build_schema_prompt(store)
    try:
        reply = ask([
            {"role": "system", "content": SUGGEST_QUESTIONS_PROMPT.format(count=count)},
            {"role": "user", "content": "TABLES:\n" + tables_text},
        ])
    except Exception as error:
        log.info("SUGGEST   skipped: %s", str(error).splitlines()[0][:120])
        return []

    lines = [re.sub(r"^[\s\-\*\d\.\)]+", "", line).strip() for line in reply.splitlines()]
    questions = [q for q in lines if q.endswith("?") and len(q) <= 150][:count]
    log.info("SUGGEST   %d question(s)", len(questions))
    return questions
