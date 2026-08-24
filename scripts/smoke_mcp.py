#!/usr/bin/env python3
"""Call the MCP server over HTTP and show what each identity gets back.

Works against a local container or the deployed Gateway, with or without a token:

  smoke_mcp.py --url http://localhost:8000/mcp        # no token -> business fallback
  smoke_mcp.py --okta                                 # browser login, deployed Gateway
  smoke_mcp.py --okta --token "$(cat build/.okta_token)"   # reuse a cached token
  smoke_mcp.py --gateway --token "$ACCESS_TOKEN"      # bring your own token

This is the functional check that replaces a test suite: it prints the resolved role and
the column set per tier, which is exactly the claim the demo makes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)
OUTPUTS = Path(__file__).resolve().parent.parent / "build" / "outputs.json"


def load_outputs() -> dict:
    if not OUTPUTS.exists():
        sys.exit(f"{OUTPUTS} not found - deploy first")
    return json.loads(OUTPUTS.read_text())


def okta_token(out: dict) -> str:
    """Sign in as a real Okta user and return that user's own access token.

    Note what is NOT returned: the exchanged token. The gateway's inbound authorizer
    validates the user's token, then AgentCore Identity performs the on-behalf-of
    exchange itself and forwards the result to the runtime. The client never sees or
    handles the exchanged token, which is the whole point of the topology - so this
    function deliberately stops at the user's token.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import probe_token  # sibling script; reuses one PKCE implementation

    issuer = os.environ.get("OKTA_ISSUER") or out.get("okta_issuer")
    spa = os.environ.get("OKTA_SPA_CLIENT_ID") or out.get("okta_spa_client_id")
    redirect = (os.environ.get("OKTA_REDIRECT_URI") or out.get("okta_redirect_uri")
                or "http://localhost:8400/callback")
    scope = (os.environ.get("OKTA_LOGIN_SCOPE") or out.get("okta_login_scope")
             or "openid api")
    if not issuer or not spa:
        sys.exit("no Okta issuer/client id - source config.env or deploy the okta step")
    token = probe_token.login(issuer, spa, redirect, scope, 900)

    # Cache it so iterating does not mean logging in through a browser every time.
    # Gitignored, mode 600, and short-lived because the token itself expires.
    cache = OUTPUTS.parent / ".okta_token"
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(token)
        cache.chmod(0o600)
        print(f'{DIM}token cached: rerun with --token "$(cat {cache})"{RESET}')
    except OSError:
        pass
    return token


DEBUG = False


def unwrap(result) -> dict:
    """Pull the JSON payload out of an MCP tool result."""
    if DEBUG:
        print(f"{DIM}  RAW isError={getattr(result, 'isError', None)!r}{RESET}")
        print(f"{DIM}  RAW structuredContent={getattr(result, 'structuredContent', None)!r}{RESET}")
        for block in (getattr(result, "content", None) or []):
            print(f"{DIM}  RAW content={getattr(block, 'text', block)!r}{RESET}")
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    return {}


async def run(url: str, token: str | None, table: str, sql: str) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    print(f"{DIM}connecting to {url}{'  (no token)' if not token else ''}{RESET}")

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)

            # Tools may be namespaced by whatever fronts the server: the AgentCore
            # Gateway uses "<targetName>___<toolName>", LiteLLM uses "<serverName>-<toolName>".
            # Resolve the real name instead of assuming the bare one.
            def tool(short: str) -> str:
                for n in names:
                    if n == short or n.endswith(f"___{short}") or n.endswith(f"-{short}"):
                        return n
                return short

            async def call(short: str, args: dict | None = None):
                return unwrap(await session.call_tool(tool(short), args or {}))
            print(f"{GREEN}ok{RESET} {len(names)} tools available")
            demo = [n for n in names if n.split("___")[-1] in
                    ("whoami", "list_tables", "describe_table", "query_data")]
            upstream = [n for n in names if n not in demo]
            print(f"   demo tools:     {', '.join(demo)}")
            print(f"   {DIM}aws dataprocessing: {', '.join(upstream[:6])}"
                  f"{' ...' if len(upstream) > 6 else ''}{RESET}")

            who = await call("whoami")
            ident = who.get("identity", {})
            data = who.get("data") or {}
            print(f"\n{BOLD}identity{RESET}")
            print(f"   resolved role : {BOLD}{ident.get('resolved_role')}{RESET}")
            print(f"   tier          : {ident.get('tier')}")
            print(f"   strategy      : {ident.get('strategy')}")
            if ident.get("fallback"):
                print(f"   {YELLOW}fallback      : {ident.get('fallback_reason')}{RESET}")
            tok = ident.get("token", {})
            print(f"   token         : present={tok.get('present')} "
                  f"issuer_ok={tok.get('issuer_matches')} "
                  f"groups={', '.join(tok.get('groups') or []) or '-'}")
            print(f"   allowed models: {', '.join(data.get('allowed_models', [])) or '-'}")

            listed = await call("list_tables")
            print(f"\n{BOLD}tables{RESET}")
            for t in (listed.get("data") or {}).get("tables", []):
                print(f"   {t['name']:<14} {len(t['visible_columns'])} cols: "
                      f"{', '.join(t['visible_columns'])}")

            desc = await call("describe_table", {"table_name": table})
            dd = desc.get("data") or {}
            if dd:
                cols = [c["name"] for c in dd.get("columns", [])]
                print(f"\n{BOLD}describe_table({table}){RESET}\n   {', '.join(cols)}")
            elif desc.get("error"):
                print(f"\n{RED}describe_table failed{RESET}: {desc['error']}")

            q = await call("query_data", {"sql": sql})
            print(f"\n{BOLD}query_data{RESET}  {DIM}{sql}{RESET}")
            if q.get("error"):
                print(f"   {RED}{q['error']['kind']}{RESET}: {q['error']['message'][:300]}")
                return 1
            qd = q.get("data") or {}
            print(f"   columns ({qd.get('column_count')}): {', '.join(qd.get('columns', []))}")
            print(f"   rows: {qd.get('row_count')}")
            for row in (qd.get("rows") or [])[:3]:
                print(f"     {DIM}{row}{RESET}")

            sensitive = [c for c in qd.get("columns", [])
                         if c in ("customer_id", "ssn_last4", "rate")]
            verdict = (f"{YELLOW}includes sensitive columns: {', '.join(sensitive)}{RESET}"
                       if sensitive else f"{GREEN}no sensitive columns present{RESET}")
            print(f"\n   -> {verdict}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="MCP endpoint; defaults to the local server")
    ap.add_argument("--gateway", action="store_true", help="use the deployed Gateway URL")
    ap.add_argument("--okta", action="store_true",
                    help="use the Okta gateway and sign in through the browser (OBO track)")
    ap.add_argument("--debug", action="store_true",
                    help="dump each raw MCP tool result before parsing")
    ap.add_argument("--compare", action="store_true",
                    help="not supported: comparing tiers needs two separate --okta runs")
    ap.add_argument("--token", help="use this bearer token verbatim")
    ap.add_argument("--table", default="customers")
    # ORDER BY makes the two tiers return the SAME row, so the only visible difference
    # is the missing column. Without it, LIMIT picks different rows each run and it looks
    # like different data rather than the same data filtered.
    ap.add_argument("--sql", default="SELECT * FROM transactions ORDER BY transaction_id LIMIT 3")
    args = ap.parse_args()

    global DEBUG
    DEBUG = args.debug

    out = load_outputs()
    if args.okta:
        url = args.url or out.get("okta_gateway_mcp_url")
        if not url:
            sys.exit("no Okta gateway URL in outputs.json - run ./deploy.sh --only gateway")
    else:
        url = args.url or (out.get("gateway_mcp_url") if args.gateway
                           else "http://localhost:8000/mcp")
    if not url:
        sys.exit("no URL: pass --url, or deploy the Gateway first")

    if args.compare:
        # A side-by-side diff needs two users' tokens at once, and the authorization code
        # flow needs a browser per user. Run this twice instead of pretending to automate
        # it; verify_permissions.py is the automated per-tier column diff.
        sys.exit("--compare cannot drive two browser logins: run --okta twice, signing in "
                 "as a different Okta user each time (use a private window), or run "
                 "python3 scripts/verify_permissions.py for the automated tier diff")

    token = args.token
    if not token and args.okta:
        token = okta_token(out)

    return asyncio.run(run(url, token, args.table, args.sql))


if __name__ == "__main__":
    sys.exit(main())
