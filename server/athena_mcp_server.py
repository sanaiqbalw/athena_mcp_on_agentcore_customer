#!/usr/bin/env python3
"""Athena MCP server, hosted on Bedrock AgentCore Runtime.

Two jobs:

  1. Re-host the AWS Data Processing MCP server (Glue, Athena, EMR tools) over
     streamable HTTP on 0.0.0.0:8000/mcp, which is what AgentCore Runtime expects.
  2. Make every one of its AWS calls run as the *calling user's* IAM role, so Lake
     Formation decides which columns come back.

Job 2 is the whole point of the demo and it is done in aws_context.py by patching the
upstream client factory - no forked code, no column logic in this server. A handful of
thin demo tools (whoami / list_tables / describe_table / query_data) sit alongside the
upstream toolset so the story is easy to tell in a single tool call.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

# Make sibling modules importable regardless of how the container invokes this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aws_context  # noqa: E402
import config as config_mod  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(f"{os.environ.get('RESOURCE_PREFIX') or 'mcp'}.server")

CFG = config_mod.load()

# Patch before the handlers are constructed: they capture their clients in __init__.
aws_context.install(CFG)

from awslabs.aws_dataprocessing_mcp_server.handlers.athena.athena_data_catalog_handler import (  # noqa: E402
    AthenaDataCatalogHandler,
)
from awslabs.aws_dataprocessing_mcp_server.handlers.athena.athena_query_handler import (  # noqa: E402
    AthenaQueryHandler,
)
from awslabs.aws_dataprocessing_mcp_server.handlers.athena.athena_workgroup_handler import (  # noqa: E402
    AthenaWorkGroupHandler,
)
from awslabs.aws_dataprocessing_mcp_server.handlers.glue.data_catalog_handler import (  # noqa: E402
    GlueDataCatalogHandler,
)
from awslabs.aws_dataprocessing_mcp_server.utils.sql_analyzer import SqlAnalyzer  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP(
    "athena-mcp",
    instructions=(
        "Query the demo data lake through Amazon Athena. Which columns you can see depends "
        "on the identity you signed in with: AWS Lake Formation filters results per IAM "
        "role. Every response reports the role it was executed as."
    ),
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
)

# Read-only, but allowed to return query results. allow_write stays False so no caller can
# mutate the catalog; allow_sensitive_data_access is required for get-query-results, which
# the demo needs in order to show rows at all.
_HANDLER_KWARGS = {"allow_write": False, "allow_sensitive_data_access": True}
GlueDataCatalogHandler(mcp, **_HANDLER_KWARGS)
AthenaQueryHandler(mcp, **_HANDLER_KWARGS)
AthenaDataCatalogHandler(mcp, **_HANDLER_KWARGS)
AthenaWorkGroupHandler(mcp, **_HANDLER_KWARGS)


# --- envelope ----------------------------------------------------------------------


def _identity_block() -> dict[str, Any]:
    scope = aws_context.current_scope()
    if scope is None:
        return {"resolved_role": "unknown", "strategy": "no_request_scope"}
    return scope.identity.as_dict()


def ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "identity": _identity_block(), "data": payload, "error": None}


def err(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "identity": _identity_block(),
        "data": None,
        "error": {"kind": kind, "message": message, **extra},
    }


def _client(service: str):
    scope = aws_context.current_scope()
    if scope is None:
        raise RuntimeError("no request scope")
    return scope.client(service)


# --- demo tools --------------------------------------------------------------------


@mcp.tool()
def whoami() -> dict:
    """Show which identity and IAM role this request is running as.

    Useful first call in a demo: it names the tier, the assumed role, how the tier was
    resolved, and which Bedrock models that tier is allowed to invoke.
    """
    scope = aws_context.current_scope()
    identity = _identity_block()
    tier = identity.get("tier", config_mod.FALLBACK_TIER)
    return ok(
        {
            "tier": tier,
            "allowed_models": list(CFG.models_for(tier)),
            "glue_database": CFG.glue_database,
            "athena_workgroup": CFG.athena_workgroup,
            "observed_request_headers": scope.header_names if scope else [],
            "note": (
                "Column visibility is enforced by AWS Lake Formation against this role. "
                "The server applies no column logic of its own."
            ),
        }
    )


@mcp.tool()
def list_tables() -> dict:
    """List the tables in the demo database that this identity is allowed to see."""
    try:
        paginator = _client("glue").get_paginator("get_tables")
        tables = []
        for page in paginator.paginate(DatabaseName=CFG.glue_database):
            for t in page.get("TableList", []):
                sd = t.get("StorageDescriptor", {})
                tables.append(
                    {
                        "name": t["Name"],
                        "visible_columns": [c["Name"] for c in sd.get("Columns", [])]
                        + [k["Name"] for k in t.get("PartitionKeys", [])],
                    }
                )
        return ok({"database": CFG.glue_database, "tables": tables})
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
        return err("glue_error", str(exc))


@mcp.tool()
def describe_table(table_name: str) -> dict:
    """Show the columns of one table, as visible to this identity.

    Columns withheld by Lake Formation are simply absent - that is the demo.
    """
    try:
        table = _client("glue").get_table(DatabaseName=CFG.glue_database, Name=table_name)["Table"]
    except Exception as exc:  # noqa: BLE001
        return err("glue_error", str(exc), table=table_name)

    sd = table.get("StorageDescriptor", {})
    columns = [{"name": c["Name"], "type": c["Type"]} for c in sd.get("Columns", [])]
    partitions = [{"name": k["Name"], "type": k["Type"]} for k in table.get("PartitionKeys", [])]
    return ok(
        {
            "table": table_name,
            "columns": columns,
            "partition_keys": partitions,
            "visible_column_count": len(columns) + len(partitions),
        }
    )


@mcp.tool()
def query_data(sql: str) -> dict:
    """Run a read-only SQL query against the demo database through Athena.

    Runs under this identity's IAM role, so Lake Formation decides which columns come
    back. `SELECT *` is expanded to the permitted columns only.
    """
    if not SqlAnalyzer.is_read_only_query(sql):
        return err("invalid_argument", "only read-only queries are permitted")

    athena = _client("athena")
    try:
        qid = athena.start_query_execution(
            QueryString=sql,
            WorkGroup=CFG.athena_workgroup,
            QueryExecutionContext={"Database": CFG.glue_database},
        )["QueryExecutionId"]
    except Exception as exc:  # noqa: BLE001
        return err("athena_error", str(exc))

    deadline = time.monotonic() + CFG.athena_timeout_seconds
    delay = 0.4
    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", state)
            kind = (
                "lake_formation_denied"
                if any(s in reason.lower() for s in ("not authorized", "insufficient permissions",
                                                     "column", "access denied"))
                else "athena_failed"
            )
            return err(kind, reason, query_execution_id=qid)
        if time.monotonic() >= deadline:
            try:
                athena.stop_query_execution(QueryExecutionId=qid)
            except Exception:  # noqa: BLE001 - best effort, stops the billing clock
                pass
            return err("athena_timeout",
                       f"query did not finish within {CFG.athena_timeout_seconds}s",
                       query_execution_id=qid)
        time.sleep(delay)
        delay = min(delay * 2, 2.0)

    columns: list[str] = []
    rows: list[list[str | None]] = []
    truncated = False
    first_page = True
    for page in athena.get_paginator("get_query_results").paginate(
        QueryExecutionId=qid, PaginationConfig={"PageSize": 1000}
    ):
        result_set = page["ResultSet"]
        if not columns:
            columns = [c["Name"] for c in result_set["ResultSetMetadata"]["ColumnInfo"]]
        page_rows = result_set.get("Rows", [])
        # Athena repeats the column names as row 0 of the first page for SELECT.
        if first_page and page_rows:
            head = [d.get("VarCharValue") for d in page_rows[0].get("Data", [])]
            if head == columns:
                page_rows = page_rows[1:]
            first_page = False
        for row in page_rows:
            if len(rows) >= CFG.athena_max_rows:
                truncated = True
                break
            rows.append([d.get("VarCharValue") for d in row.get("Data", [])])
        if truncated:
            break

    return ok(
        {
            "query_execution_id": qid,
            "columns": columns,
            "column_count": len(columns),
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    )


# --- ASGI plumbing -----------------------------------------------------------------


def header_capture_middleware(app):
    """Pure ASGI middleware: bind a RequestScope for the duration of each request.

    Deliberately not a Starlette BaseHTTPMiddleware - that runs the downstream app in a
    separate task, which would break ContextVar propagation into the tool functions.
    """

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else None
        request_scope = aws_context.RequestScope(CFG, bearer, sorted(headers))
        token = aws_context.set_scope(request_scope)
        try:
            await app(scope, receive, send)
        finally:
            aws_context.reset_scope(token)

    return wrapped


def build_app():
    return header_capture_middleware(mcp.streamable_http_app())


app = build_app()


if __name__ == "__main__":
    import uvicorn

    log.info(
        "starting on 0.0.0.0:8000/mcp | db=%s workgroup=%s region=%s",
        CFG.glue_database, CFG.athena_workgroup, CFG.region,
    )
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level=os.environ.get("LOG_LEVEL", "info").lower())
