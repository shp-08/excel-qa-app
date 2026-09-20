"""
Load the sample files through ingest.py and print what came out.
No model involved. Run:  python scripts/check_ingest.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ingest import DataStore, compute_join_hints, ingest_file  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

store = DataStore()
for path in sorted(SAMPLES.glob("*.xlsx")):
    created = ingest_file(store, path.name, path.read_bytes())
    print(f"{path.name}: {created}")

print()
for t in store.tables.values():
    print(f"== {t.name}  ({t.file} / {t.sheet}, {t.row_count} rows)")
    for c, ty in zip(t.columns, t.types):
        orig = t.original_columns.get(c, "")
        tag = f"  (was '{orig}')" if orig != c else ""
        print(f"   {c:<20} {ty}{tag}")
    print(t.sample.to_string(index=False))
    print()

hints = compute_join_hints(store)
print("join hints:")
for h in hints:
    print(f"   {h.left_table}.{h.left_col}  <->  {h.right_table}.{h.right_col}   overlap={h.overlap}")

print()
print("cross-file query check: sales per segment")
print(store.con.execute("""
    SELECT c.segment, ROUND(SUM(o.total_sales), 2) AS sales
    FROM orders_orders_2024 o
    JOIN customers_customers c ON o.cust_id = c.customerid
    WHERE o.status = 'Delivered'
    GROUP BY c.segment ORDER BY sales DESC
""").df().to_string(index=False))
