"""
Generate deliberately messy sample Excel files for testing.

  samples/customers.xlsx   sheets: Customers, Segments
  samples/orders.xlsx      sheets: Orders 2024 (title row + blank row above header,
                           money stored as text), Products
  samples/employees.xlsx   single sheet, salary as '$1,234', dates as text,
                           a duplicate column name and an unnamed column

Run:  python scripts/make_samples.py
"""

import random
from pathlib import Path

import pandas as pd

random.seed(42)
OUT = Path(__file__).resolve().parent.parent / "samples"
OUT.mkdir(exist_ok=True)

# ---------------------------------------------------------------- customers
segments = ["Enterprise", "SMB", "Consumer"]
regions = ["North", "South", "East", "West"]
customers = pd.DataFrame({
    "CustomerID": [f"C{1000 + i}" for i in range(40)],
    "Customer Name": [f"Customer {i}" for i in range(40)],
    "Segment": [random.choice(segments) for _ in range(40)],
    "Region": [random.choice(regions) for _ in range(40)],
    "Signup Date": pd.date_range("2022-01-01", periods=40, freq="17D"),
})
segment_info = pd.DataFrame({
    "Segment": segments,
    "Discount %": [15, 10, 0],
    "Account Manager": ["Priya", "Arjun", "Meera"],
})
with pd.ExcelWriter(OUT / "customers.xlsx") as xw:
    customers.to_excel(xw, sheet_name="Customers", index=False)
    segment_info.to_excel(xw, sheet_name="Segments", index=False)

# ---------------------------------------------------------------- orders
products = pd.DataFrame({
    "SKU": [f"P{100 + i}" for i in range(8)],
    "Product": ["Laptop", "Monitor", "Keyboard", "Mouse", "Dock", "Headset", "Webcam", "Cable"],
    "Category": ["Hardware"] * 5 + ["Audio", "Video", "Accessory"],
    "Unit Price": [1200, 300, 80, 40, 150, 120, 90, 15],
})
orders = pd.DataFrame({
    "Order No": [f"O{5000 + i}" for i in range(300)],
    "cust_id": [random.choice(customers["CustomerID"]) for _ in range(300)],
    "sku": [random.choice(products["SKU"]) for _ in range(300)],
    "Order Date": [
        (pd.Timestamp("2024-01-01") + pd.Timedelta(days=random.randint(0, 364))).strftime("%d/%m/%Y")
        for _ in range(300)
    ],
    "Qty": [random.randint(1, 10) for _ in range(300)],
    "Status": [random.choice(["Delivered", "Delivered", "Delivered", "Cancelled", "Pending"]) for _ in range(300)],
})
price = products.set_index("SKU")["Unit Price"]
orders["Total Sales ($)"] = [
    f"${q * price[s]:,.2f}" for q, s in zip(orders["Qty"], orders["sku"])
]

with pd.ExcelWriter(OUT / "orders.xlsx") as xw:
    # title row, blank row, then the header: tests header detection
    pd.DataFrame([["Orders export - FY2024"], [None]]).to_excel(
        xw, sheet_name="Orders 2024", index=False, header=False
    )
    orders.to_excel(xw, sheet_name="Orders 2024", index=False, startrow=2)
    products.to_excel(xw, sheet_name="Products", index=False)

# ---------------------------------------------------------------- employees
depts = ["Engineering", "Sales", "Support", "Finance"]
employees = pd.DataFrame({
    "Emp ID": list(range(1, 61)),
    "Name": [f"Employee {i}" for i in range(1, 61)],
    "Department": [random.choice(depts) for _ in range(60)],
    "Region": [random.choice(regions) for _ in range(60)],
    "Salary": [f"${random.randint(40, 160) * 1000:,}" for _ in range(60)],
    "Joined": [f"2023-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}" for _ in range(60)],
    "Rating": [random.choice([3, 4, 5]) for _ in range(60)],
})
employees["Name"] = employees["Name"]  # keep
employees.insert(3, "Name ", ["dup"] * 60)          # duplicate header after cleaning
employees.insert(5, " ", [None] * 60)                # blank header, empty column (dropped)
employees.to_excel(OUT / "employees.xlsx", index=False)

print("wrote", sorted(p.name for p in OUT.glob("*.xlsx")))
