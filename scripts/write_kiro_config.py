#!/usr/bin/env python3
"""Add the demo's Okta-fronted MCP Gateway to .kiro/settings/mcp.json.

No token and no client secret land in this file. Kiro discovers Okta from the Gateway's
401 challenge, then runs the browser login itself.

Okta gates Dynamic Client Registration behind an API token, so a URL-only entry fails with
"E0000005 Invalid session". We therefore write the SPA's public client id and a redirect URI
that is registered in Okta. Neither is a secret. Pass --url-only to try DCR anyway (works
with IdPs that allow open registration, e.g. Auth0).

Merges into any existing file and leaves unrelated servers untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Prefix-driven so several copies of the demo can coexist in one mcp.json.
PREFIX = (os.environ.get("RESOURCE_PREFIX") or "mcp").strip() or "mcp"
SERVER_KEY = f"{PREFIX}-athena-mcp"

# Kiro expands ":PORT" to http://localhost:PORT/oauth/callback. Okta's redirect wildcard is
# subdomain-only, never port, so every port used here must be listed on the SPA app. Both
# :8975 (Kiro) and :8400 (scripts/probe_token.py) are registered; keeping them separate means
# a browser login from Kiro and one from the CLI can never fight over the same port.
DEFAULT_REDIRECT = ":8975"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs", type=Path, default=ROOT / "build" / "outputs.json")
    ap.add_argument("--config", type=Path, default=ROOT / ".kiro" / "settings" / "mcp.json")
    ap.add_argument("--url-only", action="store_true",
                    help="omit clientId/redirectUri and rely on Dynamic Client Registration")
    ap.add_argument("--redirect-uri", default=DEFAULT_REDIRECT)
    ap.add_argument("--remove", action="store_true", help="teardown: drop the entry")
    args = ap.parse_args()

    out = json.loads(args.outputs.read_text()) if args.outputs.exists() else {}
    url = out.get("okta_gateway_mcp_url")
    if not url and not args.remove:
        sys.exit("okta_gateway_mcp_url not in outputs.json - deploy the gateway stack first")

    config = {}
    if args.config.exists():
        try:
            config = json.loads(args.config.read_text())
        except json.JSONDecodeError:
            sys.exit(f"{args.config} is not valid JSON; fix or move it first")

    servers = config.setdefault("mcpServers", {})

    if args.remove:
        servers.pop(SERVER_KEY, None)
        print(f"removed {SERVER_KEY}")
    else:
        entry: dict = {"url": url}
        if not args.url_only:
            client_id = out.get("okta_spa_client_id")
            if not client_id:
                sys.exit("okta_spa_client_id not in outputs.json - deploy the gateway stack first")
            entry["oauth"] = {
                "clientId": client_id,
                "redirectUri": args.redirect_uri,
                "oauthScopes": out.get("okta_login_scope", "openid api").split(),
            }
        entry["disabled"] = False
        servers[SERVER_KEY] = entry
        print(f"{SERVER_KEY} -> {url}")
        if "oauth" in entry:
            print(f"  oauth clientId {entry['oauth']['clientId']} "
                  f"redirect {args.redirect_uri} scopes {entry['oauth']['oauthScopes']}")
        else:
            print("  url only - relies on Dynamic Client Registration")

    args.config.parent.mkdir(parents=True, exist_ok=True)
    args.config.write_text(json.dumps(config, indent=2) + "\n")
    print(f"wrote {args.config}")
    print(f"other servers preserved: {sorted(k for k in servers if k != SERVER_KEY) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
