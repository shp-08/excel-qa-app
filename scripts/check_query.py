"""
Test the query pipeline WITHOUT a real model.

Each test gives answer_question() a scripted fake model: a list of replies it
will return in order. That lets us force the failure cases (invented column,
DROP statement, empty result) and confirm the guards and retry loop react
correctly. Run:  python scripts/check_query.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.ingest import DataStore, compute_join_hints, ingest_file
from core.query import answer_question, check_sql, ungrounded_numbers


def load_samples() -> DataStore:
    store = DataStore()
    for path in sorted((ROOT / "samples").glob("*.xlsx")):
        ingest_file(store, path.name, path.read_bytes())
    compute_join_hints(store)
    return store


def scripted(replies: list[str]):
    """A fake model that returns the given replies one after another."""
    queue = list(replies)
    seen = []

    def ask(messages):
        seen.append(messages)
        return queue.pop(0)

    ask.seen = seen
    return ask


def main() -> None:
    store = load_samples()
    tables = list(store.tables)
    print("tables:", tables)
    t = tables[0]
    col = store.tables[t].columns[0]
    failures = 0

    def expect(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {'PASS' if condition else 'FAIL'}  {label} {detail}")
        failures += 0 if condition else 1

    print("\n1. good query on first try")
    r = answer_question(store, "how many rows", ask=scripted([f"```sql\nSELECT COUNT(*) AS n FROM {t}\n```"]))
    expect("status ok", r.status == "ok", f"-> n={r.df.iloc[0, 0]}")
    expect("one attempt", len(r.attempts) == 1)
    expect("tables_used detected", r.tables_used == [t])

    print("\n2. invented column, then corrected")
    ask = scripted([
        f"```sql\nSELECT made_up_column FROM {t}\n```",
        f"```sql\nSELECT {col} FROM {t} LIMIT 2\n```",
    ])
    r = answer_question(store, "show something", ask=ask)
    expect("recovered", r.status == "ok" and len(r.attempts) == 2)
    expect("error was sent back to the model", "made_up_column" in ask.seen[1][-1]["content"])
    print("     error text the model saw:", r.attempts[0].error.splitlines()[0])

    print("\n3. destructive and multi-statement SQL is rejected before running")
    expect("DROP rejected", check_sql(store, f"DROP TABLE {t}") == "Only SELECT statements are allowed.")
    expect("two statements rejected", check_sql(store, f"SELECT 1; DROP TABLE {t}") == "Write exactly one SQL statement.")
    expect("file read rejected", check_sql(store, "SELECT * FROM read_csv('C:/x.csv')") is not None)
    expect("table still exists", t in [x[0] for x in store.con.execute("SHOW TABLES").fetchall()])

    print("\n4. gives up honestly after 2 retries")
    r = answer_question(store, "x", ask=scripted([f"SELECT nope FROM {t}"] * 3))
    expect("status failed", r.status == "failed" and len(r.attempts) == 3)
    expect("message explains", "could not produce" in r.message)

    print("\n5. refusal path")
    r = answer_question(store, "what is the weather", ask=scripted(["CANNOT_ANSWER: The data has no weather information."]))
    expect("status refused", r.status == "refused", f"-> {r.message}")

    r = answer_question(store, "hi", ask=scripted(["CHAT: Hello! You have customer data loaded."]))
    expect("greeting gets a chat reply, no SQL", r.status == "chat" and r.attempts == [] and r.message.startswith("Hello"))

    print("\n6. empty result triggers one hinted retry")
    ask = scripted([
        f"```sql\nSELECT * FROM {t} WHERE 1 = 0\n```",
        f"```sql\nSELECT * FROM {t} LIMIT 1\n```",
    ])
    r = answer_question(store, "x", ask=ask)
    expect("retried and got rows", r.status == "ok" and len(r.df) == 1)
    expect("hint mentions 0 rows", "0 rows" in ask.seen[1][-1]["content"])

    print("\n7. follow-up sees previous SQL")
    ask = scripted([f"```sql\nSELECT COUNT(*) AS n FROM {t}\n```"])
    answer_question(store, "and by month?", history=[{"question": "total?", "sql": "SELECT 1", "tables": [t]}], ask=ask)
    expect("history in prompt", any("SELECT 1" in m["content"] for m in ask.seen[0]))

    print("\n8. cross-file join runs (uses the first detected join hint)")
    h = store.join_hints[0]
    sql = (f"SELECT COUNT(*) AS matched FROM {h.left_table} a "
           f"JOIN {h.right_table} b ON a.{h.left_col} = b.{h.right_col}")
    r = answer_question(store, "join", ask=scripted([f"```sql\n{sql}\n```"]))
    expect("join ok", r.status == "ok", f"-> {h.left_table}.{h.left_col} = {h.right_table}.{h.right_col}, matched={r.df.iloc[0, 0]}")
    expect("both tables reported", set(r.tables_used) == {h.left_table, h.right_table})

    print("\n9. a join between unrelated columns is caught, and the model may then refuse")
    fake_join = ("SELECT e.emp_id, COUNT(o.order_no) AS n FROM employees e "
                 "LEFT JOIN orders_orders_2024 o ON CAST(e.emp_id AS VARCHAR) = o.cust_id GROUP BY e.emp_id")
    expect("invented link rejected", "matches no rows" in (check_sql(store, fake_join) or ""))
    real = store.join_hints[0]
    real_join = (f"SELECT COUNT(*) FROM {real.left_table} a JOIN {real.right_table} b "
                 f"ON a.{real.left_col} = b.{real.right_col}")
    expect("real link accepted", check_sql(store, real_join) is None)
    ask = scripted([f"```sql\n{fake_join}\n```", "CANNOT_ANSWER: Orders do not record which employee handled them."])
    r = answer_question(store, "orders per employee", ask=ask)
    expect("ends as a refusal, not a table of zeros", r.status == "refused" and len(r.attempts) == 1)

    print("\n10. number grounding check")
    import pandas as pd
    df = pd.DataFrame({"region": ["North", "South"], "total": [15230.5, 9800.0]})
    expect("true sentence passes", ungrounded_numbers("North leads with 15,230.5, South has 9800.", df) == [])
    expect("invented number caught", ungrounded_numbers("North leads with 17,000.", df) == ["17,000."])

    print(f"\n{'ALL PASSED' if failures == 0 else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
