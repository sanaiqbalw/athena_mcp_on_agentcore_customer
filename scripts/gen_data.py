#!/usr/bin/env python3
"""Generate the demo Parquet files locally under build/data/.

transactions is written as Hive partitions (year=YYYY/month=MM). customer_id in
transactions and deposits is sampled from the customers table so joins work.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

import schema

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "build" / "data"

BANKS = [
    "First National", "Cascadia Trust", "Harbor Savings", "Summit Federal",
    "Pioneer Bank", "Lakeside Credit", "Meridian Financial", "Cornerstone Bank",
]
REGIONS = ["Northeast", "Southeast", "Midwest", "Southwest", "West"]
SEGMENTS = ["Retail", "Commercial", "Wealth", "Institutional"]
TIERS = ["Bronze", "Silver", "Gold", "Platinum"]
STATUSES = ["settled", "pending", "reversed", "failed"]
PRODUCTS = ["CD-3M", "CD-6M", "CD-12M", "CD-24M", "MoneyMarket", "Savings"]
FIRST = ["Alex", "Jordan", "Casey", "Riley", "Morgan", "Taylor", "Avery", "Quinn",
         "Rowan", "Sage", "Devon", "Ellis", "Harper", "Kendall", "Logan", "Parker"]
LAST = ["Nakamura", "Okafor", "Lindqvist", "Delgado", "Whitfield", "Ferrara",
        "Bhatt", "Kowalski", "Almeida", "Yusuf", "Petrov", "Castellanos"]


def _customers(rng: random.Random, n: int) -> pa.Table:
    ids = [f"CUST-{i:06d}" for i in range(1, n + 1)]
    return pa.table(
        {
            "customer_id": ids,
            "name": [f"{rng.choice(FIRST)} {rng.choice(LAST)}" for _ in range(n)],
            "segment": [rng.choice(SEGMENTS) for _ in range(n)],
            "region": [rng.choice(REGIONS) for _ in range(n)],
            "tier": [rng.choice(TIERS) for _ in range(n)],
            "ssn_last4": [f"{rng.randint(0, 9999):04d}" for _ in range(n)],
        },
        schema=pa.schema(
            [(name, schema.arrow_type(t)) for name, t in schema.TABLES["customers"]["columns"]]
        ),
    )


def _transactions(rng: random.Random, n: int, customer_ids: list[str]) -> pa.Table:
    # Spread over the last 12 months so there is a real partition spread.
    today = dt.date.today()
    start = today - dt.timedelta(days=364)
    dates = [start + dt.timedelta(days=rng.randint(0, 364)) for _ in range(n)]
    cols = {
        "transaction_id": [f"TXN-{i:08d}" for i in range(1, n + 1)],
        "date": dates,
        "amount": [round(rng.uniform(25.0, 250_000.0), 2) for _ in range(n)],
        "bank_name": [rng.choice(BANKS) for _ in range(n)],
        "customer_id": [rng.choice(customer_ids) for _ in range(n)],
        "status": [rng.choices(STATUSES, weights=[85, 8, 4, 3])[0] for _ in range(n)],
        "region": [rng.choice(REGIONS) for _ in range(n)],
        # partition columns, derived from date so they can never disagree with the row
        "year": [f"{d.year:04d}" for d in dates],
        "month": [f"{d.month:02d}" for d in dates],
    }
    fields = [(name, schema.arrow_type(t)) for name, t in schema.TABLES["transactions"]["columns"]]
    fields += [(name, schema.arrow_type(t)) for name, t in schema.TABLES["transactions"]["partition_keys"]]
    return pa.table(cols, schema=pa.schema(fields))


def _deposits(rng: random.Random, n: int, customer_ids: list[str]) -> pa.Table:
    today = dt.date.today()
    return pa.table(
        {
            "deposit_id": [f"DEP-{i:08d}" for i in range(1, n + 1)],
            "bank_name": [rng.choice(BANKS) for _ in range(n)],
            "amount": [round(rng.uniform(1_000.0, 5_000_000.0), 2) for _ in range(n)],
            "maturity_date": [today + dt.timedelta(days=rng.randint(30, 1095)) for _ in range(n)],
            "rate": [round(rng.uniform(0.5, 5.75), 3) for _ in range(n)],
            "customer_id": [rng.choice(customer_ids) for _ in range(n)],
            "product_type": [rng.choice(PRODUCTS) for _ in range(n)],
        },
        schema=pa.schema(
            [(name, schema.arrow_type(t)) for name, t in schema.TABLES["deposits"]["columns"]]
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--rows", type=int, default=schema.ROW_COUNT)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)

    customers = _customers(rng, args.rows)
    customer_ids = customers.column("customer_id").to_pylist()
    transactions = _transactions(rng, args.rows, customer_ids)
    deposits = _deposits(rng, args.rows, customer_ids)

    for name, table in (("customers", customers), ("deposits", deposits)):
        target = args.out / name
        target.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, target / f"{name}.parquet", compression="snappy")
        print(f"  {name}: {table.num_rows} rows -> {target}/{name}.parquet")

    # transactions: Hive-partitioned dataset
    tx_dir = args.out / "transactions"
    part_schema = pa.schema([("year", pa.string()), ("month", pa.string())])
    ds.write_dataset(
        transactions,
        tx_dir,
        format="parquet",
        partitioning=ds.partitioning(part_schema, flavor="hive"),
        existing_data_behavior="overwrite_or_ignore",
        basename_template="part-{i}.parquet",
    )
    parts = sorted({p.parent.relative_to(tx_dir).as_posix() for p in tx_dir.rglob("*.parquet")})
    print(f"  transactions: {transactions.num_rows} rows -> {len(parts)} partitions")
    print(f"    e.g. {parts[0]} ... {parts[-1]}")

    # Partition list for the Glue registration step
    (args.out / "partitions.txt").write_text("\n".join(parts) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
