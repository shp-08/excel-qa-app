"""Loads the sample files and prints what the loader made of them. No model needed."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from excel_qa.file_loader import DataStore, load_data_file  # noqa: E402
from excel_qa.link_detector import detect_links  # noqa: E402

store = DataStore()
for path in sorted((PROJECT_ROOT / "samples").iterdir()):
    print(f"{path.name}: {load_data_file(store, path.name, path.read_bytes())}")

for table in store.tables.values():
    print(f"\n== {table.name}  ({table.file} / {table.sheet}, {table.row_count} rows)")
    for column, column_type in zip(table.columns, table.types):
        original = table.original_columns.get(column, "")
        renamed_note = f"  (was '{original}')" if original != column else ""
        print(f"   {column:<20} {column_type}{renamed_note}")
    print(table.sample.to_string(index=False))

print("\nlinks between sheets:")
for link in detect_links(store):
    print(f"   {link.from_table}.{link.from_column}  ->  {link.to_table}.{link.to_column}   match={link.match_share}")

print("\ncross-file query check: delivered sales per segment")
print(store.con.execute("""
    SELECT c.segment, ROUND(SUM(o.total_sales), 2) AS sales
    FROM orders_orders_2024 o
    JOIN customers_customers c ON o.cust_id = c.customerid
    WHERE o.status = 'Delivered'
    GROUP BY c.segment ORDER BY sales DESC
""").df().to_string(index=False))
