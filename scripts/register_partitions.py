#!/usr/bin/env python3
"""Register Hive partitions in Glue with BatchCreatePartition.

Deterministic and idempotent: the generator already knows every (year, month) it
wrote, so we do not need MSCK REPAIR or Athena DDL permissions here.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import boto3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--database", required=True)
    ap.add_argument("--table", required=True)
    ap.add_argument("--partitions-file", required=True, type=Path)
    args = ap.parse_args()

    region = os.environ.get("AWS_REGION", "us-west-2")
    glue = boto3.Session(profile_name=os.environ.get("AWS_PROFILE")).client("glue", region_name=region)

    table = glue.get_table(DatabaseName=args.database, Name=args.table)["Table"]
    sd = table["StorageDescriptor"]
    keys = [k["Name"] for k in table.get("PartitionKeys", [])]

    entries = []
    for line in args.partitions_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        # "year=2025/month=08" -> ["2025", "08"]
        values = [seg.split("=", 1)[1] for seg in line.split("/")]
        if len(values) != len(keys):
            raise SystemExit(f"partition {line!r} does not match keys {keys}")
        entries.append(
            {
                "Values": values,
                "StorageDescriptor": {
                    **{k: v for k, v in sd.items() if k != "Location"},
                    "Location": f"s3://{args.bucket}/data/{args.table}/{line}/",
                },
            }
        )

    created = skipped = 0
    for i in range(0, len(entries), 100):  # BatchCreatePartition caps at 100
        batch = entries[i : i + 100]
        resp = glue.batch_create_partition(
            DatabaseName=args.database, TableName=args.table, PartitionInputList=batch
        )
        for err in resp.get("Errors", []):
            code = err["ErrorDetail"]["ErrorCode"]
            if code == "AlreadyExistsException":
                skipped += 1
            else:
                raise SystemExit(f"partition error: {err}")
        created += len(batch) - len(resp.get("Errors", []))

    print(f"  partitions: {created} created, {skipped} already present, {len(entries)} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
