"""Generates linked, slightly messy sample files into samples/: three Excel workbooks and one CSV."""

import random
from pathlib import Path

import pandas as pd

random.seed(42)
OUT = Path(__file__).resolve().parent.parent / "samples"
OUT.mkdir(exist_ok=True)

FIRST = ["Aarav", "Meera", "Rohan", "Ananya", "Vikram", "Priya", "Karthik", "Divya", "Arjun", "Sneha",
         "Rahul", "Kavya", "Nikhil", "Pooja", "Sanjay", "Lakshmi", "Aditya", "Nisha", "Varun", "Isha"]
LAST = ["Sharma", "Iyer", "Nair", "Reddy", "Patel", "Menon", "Gupta", "Rao", "Das", "Kapoor", "Pillai", "Joshi"]
COMPANY_A = ["Blue", "Green", "North", "Silver", "Bright", "Urban", "Prime", "Coastal", "Summit", "Maple"]
COMPANY_B = ["Harbor", "Field", "Bridge", "Stone", "Leaf", "Peak", "River", "Gate", "Works", "Point"]
COMPANY_C = ["Traders", "Systems", "Foods", "Logistics", "Retail", "Labs", "Textiles", "Studios"]


def unique_names(count: int, make) -> list[str]:
    names: list[str] = []
    while len(names) < count:
        name = make()
        if name not in names:
            names.append(name)
    return names


# ---------------------------------------------------------------- customers
segments = ["Enterprise", "SMB", "Consumer"]
regions = ["North", "South", "East", "West"]
customers = pd.DataFrame({
    "CustomerID": [f"C{1000 + i}" for i in range(40)],
    "Customer Name": unique_names(40, lambda: f"{random.choice(COMPANY_A)}{random.choice(COMPANY_B).lower()} {random.choice(COMPANY_C)}"),
    "Segment": [random.choice(segments) for _ in range(40)],
    "Region": [random.choice(regions) for _ in range(40)],
    "Signup Date": pd.date_range("2022-01-01", periods=40, freq="17D"),
})
segment_info = pd.DataFrame({
    "Segment": segments,
    "Discount %": [15, 10, 0],
    "Account Manager": ["Priya Menon", "Arjun Rao", "Meera Iyer"],
})
with pd.ExcelWriter(OUT / "customers.xlsx") as xw:
    customers.to_excel(xw, sheet_name="Customers", index=False)
    segment_info.to_excel(xw, sheet_name="Segments", index=False)

# ---------------------------------------------------------------- employees
depts = ["Sales", "Sales", "Engineering", "Support", "Finance"]        # Sales twice: more reps
employees = pd.DataFrame({
    "Emp ID": [f"E{i:03d}" for i in range(1, 61)],
    "Name": unique_names(60, lambda: f"{random.choice(FIRST)} {random.choice(LAST)}"),
    "Department": [random.choice(depts) for _ in range(60)],
    "Region": [random.choice(regions) for _ in range(60)],
    "Salary": [f"${random.randint(40, 160) * 1000:,}" for _ in range(60)],
    "Joined": [f"2023-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}" for _ in range(60)],
    "Rating": [random.choice([3, 4, 5]) for _ in range(60)],
})
sales_reps = employees.loc[employees["Department"] == "Sales", "Emp ID"].tolist()
employees.insert(4, " ", [None] * 60)                # blank header, empty spacer column (dropped on load)
employees.to_excel(OUT / "employees.xlsx", index=False)

# ---------------------------------------------------------------- orders
products = pd.DataFrame({
    "SKU": [f"P{100 + i}" for i in range(8)],
    "Product": ["Laptop", "Monitor", "Keyboard", "Mouse", "Dock", "Headset", "Webcam", "Cable"],
    "Category": ["Hardware"] * 5 + ["Audio", "Video", "Accessory"],
    "Unit Price": [1200, 300, 80, 40, 150, 120, 90, 15],
})
# a few reps sell much more than the rest, so "top sales rep" has a clear answer
rep_weights = [6 if i < 3 else 1 for i in range(len(sales_reps))]
orders = pd.DataFrame({
    "Order No": [f"O{5000 + i}" for i in range(300)],
    "cust_id": [random.choice(customers["CustomerID"]) for _ in range(300)],
    "sku": [random.choice(products["SKU"]) for _ in range(300)],
    "Sales Rep": random.choices(sales_reps, weights=rep_weights, k=300),
    "Order Date": [
        (pd.Timestamp("2024-01-01") + pd.Timedelta(days=random.randint(0, 364))).strftime("%d/%m/%Y")
        for _ in range(300)
    ],
    "Qty": [random.randint(1, 10) for _ in range(300)],
    "Status": [random.choice(["Delivered", "Delivered", "Delivered", "Cancelled", "Pending"]) for _ in range(300)],
})
price = products.set_index("SKU")["Unit Price"]
orders["Total Sales ($)"] = [f"${q * price[s]:,.2f}" for q, s in zip(orders["Qty"], orders["sku"])]

with pd.ExcelWriter(OUT / "orders.xlsx") as xw:
    # a title row and a blank row above the header, like a real export
    pd.DataFrame([["Orders export - FY2024"], [None]]).to_excel(
        xw, sheet_name="Orders 2024", index=False, header=False
    )
    orders.to_excel(xw, sheet_name="Orders 2024", index=False, startrow=2)
    products.to_excel(xw, sheet_name="Products", index=False)

# ---------------------------------------------------------------- returns (a CSV that links to the Excel orders)
delivered = orders[orders["Status"] == "Delivered"].sample(n=45, random_state=7)
reasons = ["Damaged", "Wrong item", "Changed mind", "Late delivery", "Defective"]
returns = pd.DataFrame({
    "Return ID": [f"R{900 + i}" for i in range(len(delivered))],
    "Order No": delivered["Order No"].tolist(),
    "Return Date": [
        (pd.to_datetime(d, format="%d/%m/%Y") + pd.Timedelta(days=random.randint(3, 25))).strftime("%d/%m/%Y")
        for d in delivered["Order Date"]
    ],
    "Reason": random.choices(reasons, weights=[3, 2, 4, 2, 3], k=len(delivered)),
    "Refund Amount": delivered["Total Sales ($)"].tolist(),          # money as text, like the orders sheet
})
returns.to_csv(OUT / "returns.csv", index=False)

print("wrote", sorted(p.name for p in OUT.iterdir()), f"| {len(sales_reps)} sales reps, {len(returns)} returns")
