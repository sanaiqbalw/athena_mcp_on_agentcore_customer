#!/usr/bin/env python3
"""The gate. Assume each tier role directly and diff the columns Athena returns.

This proves Lake Formation is doing the filtering with no Gateway, no Runtime and no
Kiro in the picture. If this does not pass, nothing downstream is worth demoing.

Exit codes:
  0  analytics sees everything, business is missing exactly the restricted columns
  1  a restricted column leaked to business, analytics lost a column, or the two
     tiers saw identical columns (which means nothing is being filtered)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schema  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def load_outputs(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"{path} not found - deploy the stacks first")
    return json.loads(path.read_text())


def assume(tier: str, role_arn: str, region: str) -> boto3.Session:
    sts = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=region).client("sts")
    session_name = f"{os.environ.get('RESOURCE_PREFIX') or 'mcp'}-verify-{tier}"[:64]
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def athena_columns(session, workgroup: str, database: str, table: str, timeout: float = 90.0):
    """Return (columns, error). Columns come from ResultSetMetadata, not row 0."""
    athena = session.client("athena")
    try:
        qid = athena.start_query_execution(
            QueryString=f'SELECT * FROM "{table}" LIMIT 1',
            WorkGroup=workgroup,
            QueryExecutionContext={"Database": database},
        )["QueryExecutionId"]
    except ClientError as exc:
        return [], f"start failed: {exc.response['Error']['Code']}"

    deadline, delay = time.monotonic() + timeout, 0.5
    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            return [], status.get("StateChangeReason", state)
        if time.monotonic() >= deadline:
            return [], f"timed out after {timeout}s (query {qid})"
        time.sleep(delay)
        delay = min(delay * 2, 2.0)

    meta = athena.get_query_results(QueryExecutionId=qid, MaxResults=1)["ResultSet"]
    return [c["Name"] for c in meta["ResultSetMetadata"]["ColumnInfo"]], None


def glue_columns(session, database: str, table: str):
    try:
        t = session.client("glue").get_table(DatabaseName=database, Name=table)["Table"]
    except ClientError as exc:
        return [], exc.response["Error"]["Code"]
    sd = t.get("StorageDescriptor", {})
    return [c["Name"] for c in sd.get("Columns", [])] + [
        k["Name"] for k in t.get("PartitionKeys", [])
    ], None


def observe(tier: str, role_arn: str, region: str, workgroup: str, database: str) -> dict:
    session = assume(tier, role_arn, region)
    result = {}
    for table in schema.TABLES:
        a_cols, a_err = athena_columns(session, workgroup, database, table)
        g_cols, g_err = glue_columns(session, database, table)
        result[table] = {
            "athena": a_cols,
            "athena_error": a_err,
            "glue": g_cols,
            "glue_error": g_err,
            # union: a column visible through either surface counts as visible
            "seen": sorted(set(a_cols) | set(g_cols)),
        }
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs", type=Path,
                    default=Path(__file__).resolve().parent.parent / "build" / "outputs.json")
    ap.add_argument("--smoke", action="store_true",
                    help="also check that the resources and LF registration exist")
    args = ap.parse_args()

    out = load_outputs(args.outputs)
    region = out.get("region", os.environ.get("AWS_REGION", "us-west-2"))
    database = out["glue_database"]
    workgroup = out["athena_workgroup"]
    roles = {"analytics": out["analytics_role_arn"], "business": out["business_role_arn"]}

    if args.smoke:
        rc = smoke(region, out)
        if rc:
            return rc

    print(f"\ndatabase {database} · workgroup {workgroup} · {region}\n")
    observed = {}
    for tier, arn in roles.items():
        print(f"{DIM}assuming {arn}{RESET}")
        observed[tier] = observe(tier, arn, region, workgroup, database)

    failures: list[str] = []

    for table in schema.TABLES:
        declared = schema.column_names(table)
        restricted = schema.restricted_in(table)
        a = observed["analytics"][table]
        b = observed["business"][table]

        print(f"\n{table}")
        print(f"  declared   ({len(declared)}): {', '.join(declared)}")
        for tier, obs in (("analytics", a), ("business", b)):
            err = obs["athena_error"]
            marker = f" {YELLOW}[athena: {err}]{RESET}" if err else ""
            print(f"  {tier:<10} ({len(obs['seen'])}): {', '.join(obs['seen']) or '-'}{marker}")

        withheld = sorted(set(a["seen"]) - set(b["seen"]))
        print(f"  withheld from business: {', '.join(withheld) or '(none)'}")

        # 1. analytics must see everything
        missing = sorted(set(declared) - set(a["seen"]))
        if missing:
            failures.append(f"{table}: analytics cannot see {missing}")

        # 2. business must not see any restricted column
        leaked = sorted(set(restricted) & set(b["seen"]))
        if leaked:
            failures.append(f"{table}: RESTRICTED COLUMNS LEAKED to business: {leaked}")

        # 3. and the tiers must actually differ where the table has restricted columns
        if restricted and set(a["seen"]) == set(b["seen"]):
            failures.append(f"{table}: both tiers saw identical columns - nothing is filtering")

        # 4. business should still be able to query what it is allowed to see
        if b["athena_error"] and not leaked:
            failures.append(f"{table}: business SELECT * failed: {b['athena_error']}")

    print()
    if failures:
        print(f"{RED}FAIL{RESET}")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"{GREEN}PASS{RESET} analytics sees all columns; business is denied "
          f"{', '.join(schema.RESTRICTED_COLUMNS)} — enforced by Lake Formation")
    return 0


def smoke(region: str, out: dict) -> int:
    """Cheap existence checks on the deployed resources."""
    sess = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=region)
    problems = []
    print("smoke checks:")

    glue = sess.client("glue")
    try:
        db = glue.get_database(Name=out["glue_database"])["Database"]
        defaults = db.get("CreateTableDefaultPermissions", [])
        print(f"  {GREEN}ok{RESET} glue database {db['Name']} "
              f"(CreateTableDefaultPermissions: {len(defaults)})")
        if defaults:
            problems.append("database still has CreateTableDefaultPermissions set")
    except ClientError as exc:
        problems.append(f"glue database missing: {exc}")

    for table in schema.TABLES:
        try:
            glue.get_table(DatabaseName=out["glue_database"], Name=table)
            print(f"  {GREEN}ok{RESET} table {table}")
        except ClientError:
            problems.append(f"table {table} missing")

    athena = sess.client("athena")
    try:
        wg = athena.get_work_group(WorkGroup=out["athena_workgroup"])["WorkGroup"]
        loc = wg["Configuration"]["ResultConfiguration"]["OutputLocation"]
        print(f"  {GREEN}ok{RESET} workgroup {wg['Name']} -> {loc}")
    except ClientError as exc:
        problems.append(f"workgroup missing: {exc}")

    lf = sess.client("lakeformation")
    try:
        res = lf.list_resources()["ResourceInfoList"]
        want = f":s3:::{out['data_bucket']}/data"
        hit = [r for r in res if want in r["ResourceArn"]]
        if hit:
            print(f"  {GREEN}ok{RESET} LF registered {hit[0]['ResourceArn']}")
        else:
            problems.append(f"S3 location not registered with Lake Formation ({want})")
    except ClientError as exc:
        problems.append(f"lakeformation list_resources failed: {exc}")

    # The single most dangerous misconfiguration: IAM_ALLOWED_PRINCIPALS still granted
    try:
        perms = lf.list_permissions(
            Principal={"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
            Resource={"Database": {"Name": out["glue_database"]}},
        ).get("PrincipalResourcePermissions", [])
        if perms:
            problems.append("IAM_ALLOWED_PRINCIPALS still granted on the database "
                            "- column filtering would be bypassed")
        else:
            print(f"  {GREEN}ok{RESET} no IAM_ALLOWED_PRINCIPALS grant on the database")
    except ClientError as exc:
        print(f"  {YELLOW}??{RESET} could not check IAM_ALLOWED_PRINCIPALS: {exc}")

    if problems:
        print(f"\n{RED}smoke checks failed{RESET}")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
