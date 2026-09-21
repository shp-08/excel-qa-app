"""Tests the question pipeline with a scripted fake model, so every failure case can be forced. No API key needed."""

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from excel_qa.answer_writer import numbers_not_in_result  # noqa: E402
from excel_qa.file_loader import DataStore, load_data_file  # noqa: E402
from excel_qa.link_detector import detect_links  # noqa: E402
from excel_qa.question_pipeline import answer_question  # noqa: E402
from excel_qa.sql_checks import validate_sql  # noqa: E402

failures = 0


def load_sample_store() -> DataStore:
    store = DataStore()
    for path in sorted((PROJECT_ROOT / "samples").iterdir()):
        load_data_file(store, path.name, path.read_bytes())
    detect_links(store)
    return store


def fake_model(replies: list[str]):
    """A stand-in for the model that returns the given replies in order and records what it was sent."""
    remaining = list(replies)

    def ask(messages):
        ask.received.append(messages)
        return remaining.pop(0)

    ask.received = []
    return ask


def sql_reply(sql: str) -> str:
    return f"```sql\n{sql}\n```"


def expect(label: str, condition: bool, detail: str = "") -> None:
    global failures
    print(f"  {'PASS' if condition else 'FAIL'}  {label} {detail}")
    failures += 0 if condition else 1


def main() -> None:
    store = load_sample_store()
    table = list(store.tables)[0]
    column = store.tables[table].columns[0]
    print("tables:", list(store.tables))

    print("\n1. a good query works on the first try")
    result = answer_question(store, "how many rows", ask=fake_model([sql_reply(f"SELECT COUNT(*) AS n FROM {table}")]))
    expect("status ok", result.status == "ok", f"-> n={result.rows.iloc[0, 0]}")
    expect("one attempt", len(result.attempts) == 1)
    expect("tables used are reported", result.tables_used == [table])

    print("\n2. an invented column is caught and the error goes back to the model")
    ask = fake_model([sql_reply(f"SELECT made_up_column FROM {table}"), sql_reply(f"SELECT {column} FROM {table} LIMIT 2")])
    result = answer_question(store, "show something", ask=ask)
    expect("recovered on the second attempt", result.status == "ok" and len(result.attempts) == 2)
    expect("the model saw the error", "made_up_column" in ask.received[1][-1]["content"])

    print("\n3. unsafe SQL is rejected before it runs")
    expect("DROP rejected", validate_sql(store, f"DROP TABLE {table}") == "Only SELECT statements are allowed.")
    expect("two statements rejected", validate_sql(store, f"SELECT 1; DROP TABLE {table}") == "Write exactly one SQL statement.")
    expect("reading a file rejected", validate_sql(store, "SELECT * FROM read_csv('C:/x.csv')") is not None)
    expect("table still exists", table in [row[0] for row in store.con.execute("SHOW TABLES").fetchall()])

    print("\n4. gives up honestly after the allowed retries")
    result = answer_question(store, "x", ask=fake_model([f"SELECT nope FROM {table}"] * 3))
    expect("status failed after 3 attempts", result.status == "failed" and len(result.attempts) == 3)
    expect("message explains why", "could not produce" in result.message)

    print("\n5. the model can refuse or just chat")
    result = answer_question(store, "weather?", ask=fake_model(["CANNOT_ANSWER: The data has no weather information."]))
    expect("refusal", result.status == "refused", f"-> {result.message}")
    result = answer_question(store, "hi", ask=fake_model(["CHAT: Hello! You have customer data loaded."]))
    expect("greeting gets a chat reply and no SQL", result.status == "chat" and result.attempts == [])

    print("\n6. an empty result gets one extra try with a hint")
    ask = fake_model([sql_reply(f"SELECT * FROM {table} WHERE 1 = 0"), sql_reply(f"SELECT * FROM {table} LIMIT 1")])
    result = answer_question(store, "x", ask=ask)
    expect("retried and got rows", result.status == "ok" and len(result.rows) == 1)
    expect("the hint mentions 0 rows", "0 rows" in ask.received[1][-1]["content"])

    print("\n7. a follow-up question sees the previous SQL")
    ask = fake_model([sql_reply(f"SELECT COUNT(*) AS n FROM {table}")])
    answer_question(store, "and by month?", previous_turns=[{"question": "total?", "sql": "SELECT 1", "tables": [table]}], ask=ask)
    expect("previous SQL is in the prompt", any("SELECT 1" in m["content"] for m in ask.received[0]))

    print("\n8. a cross-file join runs, using the first detected link")
    link = store.links[0]
    join_sql = (f"SELECT COUNT(*) AS matched FROM {link.from_table} a "
                f"JOIN {link.to_table} b ON a.{link.from_column} = b.{link.to_column}")
    result = answer_question(store, "join", ask=fake_model([sql_reply(join_sql)]))
    expect("join ok", result.status == "ok", f"-> matched={result.rows.iloc[0, 0]}")
    expect("both tables reported", set(result.tables_used) == {link.from_table, link.to_table})

    print("\n9. a join between unrelated columns is caught, and the model may then refuse")
    invented_join = ("SELECT e.emp_id, COUNT(o.order_no) AS n FROM employees e "
                     "LEFT JOIN orders_orders_2024 o ON CAST(e.emp_id AS VARCHAR) = o.cust_id GROUP BY e.emp_id")
    expect("invented link rejected", "matches no rows" in (validate_sql(store, invented_join) or ""))
    expect("real link accepted", validate_sql(store, join_sql) is None)
    ask = fake_model([sql_reply(invented_join), "CANNOT_ANSWER: Customers are not linked to employees."])
    result = answer_question(store, "orders per employee by customer id", ask=ask)
    expect("ends as a refusal, not a table of zeros", result.status == "refused" and len(result.attempts) == 1)

    print("\n10. numbers in the summary are checked against the result")
    rows = pd.DataFrame({"region": ["North", "South"], "total": [15230.5, 9800.0]})
    expect("a true sentence passes", numbers_not_in_result("North leads with 15,230.5, South has 9800.", rows) == [])
    expect("an invented number is caught", numbers_not_in_result("North leads with 17,000.", rows) == ["17,000."])

    print(f"\n{'ALL PASSED' if failures == 0 else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
