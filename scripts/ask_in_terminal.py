"""Asks the real model questions about the sample files, from the terminal. Pass your own questions as arguments."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from excel_qa import llm_client  # noqa: E402
from excel_qa.answer_writer import build_summary_conversation, numbers_not_in_result  # noqa: E402
from excel_qa.file_loader import DataStore, load_data_file  # noqa: E402
from excel_qa.link_detector import detect_links  # noqa: E402
from excel_qa.question_pipeline import answer_question  # noqa: E402

DEFAULT_QUESTIONS = [
    "How many customers are there in each segment?",
    "What is the total order value per customer segment?",          # needs two files
    "Now show only the top segment, broken down by month",          # follow-up
    "Which sales rep sold the most, and which region are they in?",  # needs two files
    "What is the profit margin per product?",                       # no cost column: should refuse
    "What will the weather be tomorrow?",                           # not about the data: should refuse
]


def main() -> None:
    store = DataStore()
    for path in sorted((PROJECT_ROOT / "samples").iterdir()):
        load_data_file(store, path.name, path.read_bytes())
    detect_links(store)
    print(f"model: {llm_client.main_model_name()}   tables: {', '.join(store.tables)}\n")

    previous_turns = []                        # questions run as one conversation, so follow-ups work
    for question in sys.argv[1:] or DEFAULT_QUESTIONS:
        print("=" * 90)
        print("Q:", question)
        result = answer_question(store, question, previous_turns)
        print(f"status: {result.status}   attempts: {len(result.attempts)}   {result.seconds}s")
        if result.status != "ok":
            print("->", result.message)
            continue

        print(result.sql)
        print(result.rows.head(10).to_string(index=False))
        summary = "".join(llm_client.stream_model_reply(build_summary_conversation(result)))
        print("A:", summary)
        unmatched = numbers_not_in_result(summary, result.rows)
        if unmatched:
            print("   !! numbers not found in the result:", unmatched)
        previous_turns.append({"question": question, "sql": result.sql, "tables": result.tables_used})


if __name__ == "__main__":
    main()
