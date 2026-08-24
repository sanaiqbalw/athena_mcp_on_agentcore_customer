#!/usr/bin/env python3
"""Prepare Lake Formation so column-level grants actually bite.

Two jobs, both load-bearing:

1. Append the deploying principal to the data lake admin list. Existing admins
   (DataZone, SageMaker service roles) are preserved - we read the settings,
   modify only DataLakeAdmins, and write the same object back.

2. Revoke any IAM_ALLOWED_PRINCIPALS grant on the demo database and tables. The
   account default is IAM_ALLOWED_PRINCIPALS: ALL. If that grant is present, every
   principal sees every column and the whole demo silently proves nothing. We never
   change the account-level default itself, only the grants on our own resources.

Run with --remove-admin to undo step 1 during teardown.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

IAM_ALLOWED = "IAM_ALLOWED_PRINCIPALS"


def session() -> boto3.Session:
    return boto3.Session(
        profile_name=os.environ.get("AWS_PROFILE"),
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
    )


def sync_admin(lf, account_id: str, admin_arn: str, remove: bool) -> bool:
    settings = lf.get_data_lake_settings(CatalogId=account_id)["DataLakeSettings"]
    admins = settings.get("DataLakeAdmins", [])
    current = [a["DataLakePrincipalIdentifier"] for a in admins]

    print(f"  existing data lake admins ({len(current)}):")
    for arn in current:
        print(f"    - {arn}")

    if remove:
        if admin_arn not in current:
            print(f"  {admin_arn} is not an admin, nothing to remove")
            return False
        settings["DataLakeAdmins"] = [
            a for a in admins if a["DataLakePrincipalIdentifier"] != admin_arn
        ]
        action = "removed"
    else:
        if admin_arn in current:
            print(f"  {admin_arn} is already an admin")
            return False
        settings["DataLakeAdmins"] = admins + [{"DataLakePrincipalIdentifier": admin_arn}]
        action = "appended"

    # Write the whole settings object back unchanged apart from DataLakeAdmins, so
    # CreateDatabaseDefaultPermissions / CreateTableDefaultPermissions and the
    # account parameters keep their existing values.
    lf.put_data_lake_settings(CatalogId=account_id, DataLakeSettings=settings)
    print(f"  {action} {admin_arn}")
    return True


def _resources(account_id: str, database: str, tables: list[str]) -> list[tuple[str, dict]]:
    out = [("database", {"Database": {"CatalogId": account_id, "Name": database}})]
    for table in tables:
        out.append(
            ("table " + table, {"Table": {"CatalogId": account_id, "DatabaseName": database, "Name": table}})
        )
    return out


def strip_iam_allowed(lf, account_id: str, database: str, tables: list[str]) -> int:
    revoked = 0
    for label, resource in _resources(account_id, database, tables):
        try:
            perms = lf.list_permissions(
                CatalogId=account_id,
                Principal={"DataLakePrincipalIdentifier": IAM_ALLOWED},
                Resource=resource,
            ).get("PrincipalResourcePermissions", [])
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("EntityNotFoundException", "AccessDeniedException"):
                print(f"  {label}: cannot list ({exc.response['Error']['Code']})")
                continue
            raise

        hits = [p for p in perms if p["Principal"]["DataLakePrincipalIdentifier"] == IAM_ALLOWED]
        if not hits:
            print(f"  {label}: clean")
            continue

        for p in hits:
            lf.revoke_permissions(
                CatalogId=account_id,
                Principal=p["Principal"],
                Resource=p["Resource"],
                Permissions=p.get("Permissions", []),
                PermissionsWithGrantOption=p.get("PermissionsWithGrantOption", []),
            )
            revoked += 1
            print(f"  {label}: revoked {IAM_ALLOWED} {p.get('Permissions')}")
    return revoked


def reassert_db_defaults(glue, account_id: str, database: str) -> None:
    db = glue.get_database(CatalogId=account_id, Name=database)["Database"]
    payload = {
        "Name": db["Name"],
        "Description": db.get("Description", ""),
        "LocationUri": db.get("LocationUri", ""),
        "Parameters": db.get("Parameters", {}),
        "CreateTableDefaultPermissions": [],
    }
    glue.update_database(CatalogId=account_id, Name=database, DatabaseInput=payload)
    print(f"  database {database}: CreateTableDefaultPermissions = []")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-arn", required=True)
    ap.add_argument("--database", required=True)
    ap.add_argument("--tables", default="transactions,customers,deposits")
    ap.add_argument("--remove-admin", action="store_true", help="teardown: drop the admin we added")
    ap.add_argument("--outputs", help="path to build/outputs.json to record lf_admin_appended")
    args = ap.parse_args()

    sess = session()
    account_id = sess.client("sts").get_caller_identity()["Account"]
    lf = sess.client("lakeformation")
    glue = sess.client("glue")
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]

    print("data lake admins:")
    changed = sync_admin(lf, account_id, args.admin_arn, args.remove_admin)

    if not args.remove_admin:
        print(f"\nstripping {IAM_ALLOWED} grants:")
        strip_iam_allowed(lf, account_id, args.database, tables)
        print("\nglue database defaults:")
        reassert_db_defaults(glue, account_id, args.database)

        if args.outputs and changed:
            with open(args.outputs) as fh:
                data = json.load(fh)
            data["lf_admin_appended"] = "true"
            with open(args.outputs, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
