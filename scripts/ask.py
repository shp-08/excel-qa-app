"""
Ask the REAL model questions about the sample files, from the terminal.

    python scripts/ask.py                      runs a few built-in questions
    python scripts/ask.py "your question"      asks one question

Questions are asked as one conversation, so follow-ups work.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import llm
from core.ingest import DataStore, compute_join_hints, ingest_file
from core.query import answer_question, narration_messages, ungrounded_numbers

DEFAULT_QUESTIONS = [
    "How many customers are there in each segment?",
    "What is the total order value per customer segment?",      # needs a cross-file join
    "Now show only the top segment, broken down by month",      # follow-up
    "Which product category sold the most units?",
    "What is the profit margin per product?",                   # probably unanswerable
    "What will the weather be tomorrow?",                       # definitely unanswerable
]


def main() -> None:
    store = DataStore()
    for path in sorted((ROOT / "samples").glob("*.xlsx")):
        ingest_file(store, path.name, path.read_bytes())
    compute_join_hints(store)
    print(f"model: {llm.model_name()}   tables: {', '.join(store.tables)}\n")

    history = []
    for q in sys.argv[1:] or DEFAULT_QUESTIONS:
        print("=" * 90)
        print("Q:", q)
        r = answer_question(store, q, history)
        print(f"status: {r.status}   attempts: {len(r.attempts)}   {r.seconds}s")
        for i, a in enumerate(r.attempts, 1):
            if a.error:
                print(f"  attempt {i} failed: {a.error.splitlines()[0]}")
        if r.status != "ok":
            print("->", r.message)
            continue
        print(r.sql)
        print(r.df.head(10).to_string(index=False))
        sentence = "".join(llm.chat_stream(narration_messages(r)))
        print("A:", sentence)
        bad = ungrounded_numbers(sentence, r.df)
        if bad:
            print("   !! numbers not found in result:", bad)
        history.append({"question": q, "sql": r.sql, "tables": r.tables_used})


if __name__ == "__main__":
    main()
