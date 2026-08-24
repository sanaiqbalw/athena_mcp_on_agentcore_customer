"""Single source of truth for the demo tables and the per-tier column policy.

Imported by gen_data.py and verify_permissions.py so the generator, the catalog and the
Lake Formation grants can never disagree about what a table looks like.
"""

from __future__ import annotations

import os

# column name -> (pyarrow type name, glue/athena type name)
_TYPES = {
    "string": ("string", "string"),
    "double": ("double", "double"),
    "date": ("date32", "date"),
}

TABLES: dict[str, dict] = {
    "transactions": {
        "columns": [
            ("transaction_id", "string"),
            ("date", "date"),
            ("amount", "double"),
            ("bank_name", "string"),
            ("customer_id", "string"),
            ("status", "string"),
            ("region", "string"),
        ],
        # Hive-style partitions; values are strings so month keeps its leading zero
        "partition_keys": [("year", "string"), ("month", "string")],
    },
    "customers": {
        "columns": [
            ("customer_id", "string"),
            ("name", "string"),
            ("segment", "string"),
            ("region", "string"),
            ("tier", "string"),
            ("ssn_last4", "string"),
        ],
        "partition_keys": [],
    },
    "deposits": {
        "columns": [
            ("deposit_id", "string"),
            ("bank_name", "string"),
            ("amount", "double"),
            ("maturity_date", "date"),
            ("rate", "double"),
            ("customer_id", "string"),
            ("product_type", "string"),
        ],
        "partition_keys": [],
    },
}

# What the business tier must never see. Enforced by Lake Formation, not by code.
RESTRICTED_COLUMNS = ("customer_id", "ssn_last4", "rate")

# Role names are prefix-driven so the same schema serves any deployment. The defaults
# match what the templates create for ${PREFIX}.
_PREFIX = (os.environ.get("RESOURCE_PREFIX") or "mcp").strip() or "mcp"

TIERS = {
    "analytics": {
        "role_name": os.environ.get("ANALYTICS_ROLE_NAME") or f"{_PREFIX}-analytics-role",
        "excluded": (),
    },
    "business": {
        "role_name": os.environ.get("BUSINESS_ROLE_NAME") or f"{_PREFIX}-business-role",
        "excluded": RESTRICTED_COLUMNS,
    },
}

LEAST_PRIVILEGE_TIER = "business"

ROW_COUNT = 10_000


def glue_type(logical: str) -> str:
    return _TYPES[logical][1]


def arrow_type(logical: str):
    import pyarrow as pa

    return {"string": pa.string(), "double": pa.float64(), "date32": pa.date32()}[
        _TYPES[logical][0]
    ]


def column_names(table: str, include_partitions: bool = True) -> list[str]:
    spec = TABLES[table]
    names = [name for name, _ in spec["columns"]]
    if include_partitions:
        names += [name for name, _ in spec["partition_keys"]]
    return names


def visible_columns(table: str, tier: str) -> list[str]:
    """Columns the given tier is expected to see. The demo's whole assertion."""
    excluded = set(TIERS[tier]["excluded"])
    return [c for c in column_names(table) if c not in excluded]


def restricted_in(table: str) -> list[str]:
    """Restricted columns that actually exist in this table."""
    names = set(column_names(table))
    return [c for c in RESTRICTED_COLUMNS if c in names]
