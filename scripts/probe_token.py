#!/usr/bin/env python3
"""Prove the Okta OBO chain carries enough identity for Lake Formation to act on.

Runs the real flow, no mocks: PKCE login as a live Okta user, then the RFC 8693
on-behalf-of exchange that AgentCore Gateway performs for an MCP target.

What it has to prove, and why each part is load-bearing:

  1. The exchange succeeds at all. It is the only shape where the user's identity
     survives the gateway->runtime hop as a token.
  2. sub is preserved and cid changes. That pair IS delegation: the service is
     acting, on behalf of a named user. Without it there is no per-user story.
  3. The groups claim survives onto the EXCHANGED token. This is the one that
     decides the architecture. The MCP server only ever sees the exchanged token,
     so a groups claim present on the subject token but absent after the exchange
     is useless - the server would have to fall back to an Okta directory lookup.

Lake Formation never sees an Okta user or group. It authorizes IAM principals. The
group claim only selects which tier role to assume; the column grants on those roles
are untouched, so enforcement stays entirely in Lake Formation.

  python3 scripts/probe_token.py                  # full login + exchange
  python3 scripts/probe_token.py --no-login       # skip login, exchange only (needs --subject-token)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def claims(jwt: str) -> dict:
    """Decode a JWT payload. Signature is validated by the Gateway and Runtime
    authorizers upstream; here we only need to read what the token carries."""
    part = jwt.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def post_token(issuer: str, form: dict, basic: str | None = None) -> tuple[int, object]:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    if basic:
        headers["Authorization"] = f"Basic {basic}"
    req = urllib.request.Request(
        f"{issuer}/v1/token", data=urllib.parse.urlencode(form).encode(),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:400]


def client_secret(provider: str, region: str) -> str:
    """Read the Okta client secret from the AgentCore token vault's backing secret.

    Deliberately not a config value: the secret already exists in Secrets Manager
    because the credential provider put it there, so the repo never carries a copy.
    """
    if os.environ.get("OKTA_CLIENT_SECRET"):
        return os.environ["OKTA_CLIENT_SECRET"]

    control = boto3.client("bedrock-agentcore-control", region_name=region)
    got = control.get_oauth2_credential_provider(name=provider)
    arn = got.get("clientSecretArn")
    if isinstance(arn, dict):
        arn = arn.get("secretArn")
    if not arn:
        raise SystemExit(
            f"{RED}provider {provider} exposes no client secret ARN{RESET}\n"
            f"  set OKTA_CLIENT_SECRET instead"
        )
    raw = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=arn)
    payload = raw["SecretString"]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    for key in ("client_secret", "clientSecret", "secret"):
        if key in parsed:
            return parsed[key]
    return payload


def login(issuer: str, spa_client: str, redirect: str, scope: str, timeout: int) -> str:
    """Authorization code + PKCE against Okta, capturing the callback locally."""
    verifier = b64url(secrets.token_bytes(40))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_hex(8)

    url = f"{issuer}/v1/authorize?" + urllib.parse.urlencode({
        "client_id": spa_client, "response_type": "code", "scope": scope,
        "redirect_uri": redirect, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })

    parsed = urllib.parse.urlparse(redirect)
    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            done = "code" in captured
            self.wfile.write(
                b"<h2>Signed in. Return to the terminal.</h2>" if done
                else b"<h2>No authorization code in this callback.</h2>"
            )

        def log_message(self, *args):
            pass

    print(f"\n{BOLD}Open this URL and sign in{RESET}  {DIM}(scope: {scope}){RESET}")
    print(f"\n{url}\n")
    print(f"{DIM}waiting up to {timeout}s for the callback on {redirect}...{RESET}", flush=True)

    server = http.server.HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 80), Handler)
    server.timeout = 30
    deadline = time.time() + timeout
    # Loop rather than handle a single request: browsers also fetch /favicon.ico,
    # which would otherwise consume our one and only handled request.
    while time.time() < deadline:
        server.handle_request()
        if "code" in captured or "error" in captured:
            break
    server.server_close()

    if "code" not in captured:
        raise SystemExit(f"{RED}FAIL{RESET} no authorization code received: {captured or 'timeout'}")
    if captured.get("state") != state:
        raise SystemExit(f"{RED}FAIL{RESET} state mismatch - stale callback replayed by the browser")

    status, body = post_token(issuer, {
        "grant_type": "authorization_code", "code": captured["code"],
        "redirect_uri": redirect, "client_id": spa_client, "code_verifier": verifier,
    })
    if status != 200:
        raise SystemExit(f"{RED}FAIL{RESET} code -> token: HTTP {status} {body}")
    print(f"{GREEN}ok{RESET} user access token obtained (scope: {body.get('scope')})")
    return body["access_token"]


def exchange(issuer: str, svc_client: str, secret: str, subject: str,
             scope: str, audience: str) -> str:
    """The RFC 8693 hop AgentCore performs for an OAUTH/TOKEN_EXCHANGE target.

    No actor_token is sent. That is not an omission: this authorization server grants
    the exchange only without one - every variant carrying an actor token was denied
    403 - which is why the credential provider is configured actorTokenContent=NONE.
    """
    basic = base64.b64encode(f"{svc_client}:{secret}".encode()).decode()
    status, body = post_token(issuer, {
        "grant_type": TOKEN_EXCHANGE,
        "subject_token": subject,
        "subject_token_type": ACCESS_TOKEN_TYPE,
        "scope": scope,
        "audience": audience,
    }, basic=basic)
    if status != 200:
        raise SystemExit(
            f"{RED}FAIL{RESET} exchange: HTTP {status} {body}\n"
            f"  check the access policy on {issuer} permits the Token Exchange grant\n"
            f"  for client {svc_client} with scope '{scope}'"
        )
    print(f"{GREEN}ok{RESET} exchanged token obtained (scope: {body.get('scope')})")
    return body["access_token"]


def tier_from_groups(groups) -> tuple[str, str]:
    """Map Okta groups to a tier. Least privilege wins, so a user in both groups
    resolves to business - the same fail-closed rule the server applies."""
    found = {str(g).strip().lower() for g in (groups or [])}
    if "business" in found:
        return "business", "business group present (deny wins over analytics)"
    if "analytics" in found:
        return "analytics", "analytics group"
    return "business", "no recognised group - least privilege fallback"


def report(label: str, payload: dict, groups_claim: str) -> None:
    print(f"\n{BOLD}{label}{RESET}")
    for key in ("iss", "aud", "sub", "cid", "uid", "scp", groups_claim):
        if key in payload:
            marker = f"  {YELLOW}<-- the tier signal{RESET}" if key == groups_claim else ""
            print(f"  {key:7} {payload[key]}{marker}")
    if groups_claim not in payload:
        print(f"  {groups_claim:7} {RED}ABSENT{RESET}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issuer", default=os.environ.get("OKTA_ISSUER"))
    ap.add_argument("--spa-client-id", default=os.environ.get("OKTA_SPA_CLIENT_ID"))
    ap.add_argument("--provider", default=os.environ.get("OKTA_OBO_PROVIDER") or "mcp-okta-obo")
    ap.add_argument("--audience", default=os.environ.get("OKTA_AUDIENCE"))
    ap.add_argument("--exchange-scope", default=os.environ.get("OKTA_EXCHANGE_SCOPE", "api"))
    ap.add_argument("--login-scope", default=os.environ.get("OKTA_LOGIN_SCOPE", "openid api"))
    ap.add_argument("--groups-claim", default=os.environ.get("OKTA_GROUPS_CLAIM", "groups"))
    ap.add_argument("--redirect-uri",
                    default=os.environ.get("OKTA_REDIRECT_URI", "http://localhost:8400/callback"))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--subject-token", help="skip the browser login and exchange this token")
    args = ap.parse_args()

    missing = [n for n in ("issuer", "spa_client_id", "audience")
               if not getattr(args, n)]
    if missing:
        print(f"{RED}missing configuration:{RESET} {', '.join(missing)}", file=sys.stderr)
        print("  source config.env, or pass the matching --flags", file=sys.stderr)
        return 2

    svc_secret = client_secret(args.provider, args.region)
    control = boto3.client("bedrock-agentcore-control", region_name=args.region)
    cfg = control.get_oauth2_credential_provider(name=args.provider)
    inner = (cfg.get("oauth2ProviderConfigOutput") or {}).get("customOauth2ProviderConfig") or {}
    svc_client = inner.get("clientId")
    obo = (inner.get("onBehalfOfTokenExchangeConfig") or {})
    print(f"{DIM}provider {args.provider}: client {svc_client}, "
          f"grant {obo.get('grantType')}, "
          f"actor {(obo.get('tokenExchangeGrantTypeConfig') or {}).get('actorTokenContent')}{RESET}")
    if not svc_client:
        print(f"{RED}provider has no clientId{RESET}", file=sys.stderr)
        return 2

    subject = args.subject_token or login(
        args.issuer, args.spa_client_id, args.redirect_uri, args.login_scope, args.timeout
    )
    exchanged = exchange(args.issuer, svc_client, svc_secret, subject,
                         args.exchange_scope, args.audience)

    before, after = claims(subject), claims(exchanged)
    report("SUBJECT token  (presented by the user)", before, args.groups_claim)
    report("EXCHANGED token  (all the MCP server ever sees)", after, args.groups_claim)

    same_user = before.get("sub") == after.get("sub")
    delegated = before.get("cid") != after.get("cid")
    groups = after.get(args.groups_claim)
    tier, why = tier_from_groups(groups)

    print(f"\n{BOLD}verdict{RESET}")
    print(f"  user preserved (sub)        {GREEN if same_user else RED}{same_user}{RESET}  {after.get('sub')}")
    print(f"  delegated (cid changed)     {GREEN if delegated else RED}{delegated}{RESET}  "
          f"{before.get('cid')} -> {after.get('cid')}")
    print(f"  groups on exchanged token   "
          f"{GREEN if groups else RED}{groups if groups else 'ABSENT'}{RESET}")
    print(f"  resolved tier               {BOLD}{tier}{RESET}  ({why})")

    failures = []
    if not same_user:
        failures.append("sub not preserved - the exchange lost the user, OBO is not happening")
    if not delegated:
        failures.append("cid unchanged - no delegation, this looks like a plain reissue")
    if not groups:
        failures.append(
            f"'{args.groups_claim}' absent from the exchanged token - the server cannot "
            f"resolve a tier from it. Add the claim on the authorization server with "
            f"'Include in token type: Access Token / Always' and 'Include in: Any scope', "
            f"or fall back to an Okta directory lookup keyed on sub."
        )

    if failures:
        print(f"\n{RED}FAIL{RESET}")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"\n{GREEN}PASS{RESET} the exchanged token names the user and carries the group "
          f"that selects the {tier} role; Lake Formation filters columns from there")
    return 0


if __name__ == "__main__":
    sys.exit(main())
