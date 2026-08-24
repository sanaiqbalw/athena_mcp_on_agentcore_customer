"""Work out which tier the caller belongs to, and get AWS credentials for it.

The flow is short and always ends in a decision:

  1. Decode the inbound bearer token's payload.
  2. Require its issuer to be the configured Okta issuer. Anything else is unrecognised.
  3. Read the groups claim and map it to a tier.
  4. Assume that tier's IAM role and hand back its credentials.
  5. Anything unexpected - no token, unparseable, expired, foreign issuer, unknown group -
     resolves to the least-privilege business tier, with the reason recorded.

Signature verification is done upstream twice, by the Gateway's CUSTOM_JWT authorizer and
again by the Runtime's own authorizer, so this module decodes the payload without
re-validating the signature. It still checks issuer, audience and expiry, and it never
uses the claim set to widen access - the widest thing a claim can do is select the
analytics role, which Lake Formation then constrains anyway.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

from config import FALLBACK_TIER, Config

# Resource names are prefix-driven so one account can host several copies of the demo.
# Also used for STS session names, which must stay within 64 characters.
RESOURCE_PREFIX = (os.environ.get("RESOURCE_PREFIX") or "mcp").strip() or "mcp"

log = logging.getLogger(f"{RESOURCE_PREFIX}.identity")


@dataclass
class TokenFacts:
    """What we learned about the inbound token. Names and shapes only, never values."""

    present: bool = False
    token_use: str | None = None
    issuer_matches: bool | None = None
    audience_matches: bool | None = None
    expired: bool | None = None
    claim_names: list[str] = field(default_factory=list)
    subject_present: bool = False
    issuer: str | None = None
    groups: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "token_use": self.token_use,
            "issuer_matches": self.issuer_matches,
            "audience_matches": self.audience_matches,
            "expired": self.expired,
            "claim_names": sorted(self.claim_names),
            "groups": self.groups,
        }


@dataclass
class ResolvedIdentity:
    tier: str
    role_arn: str
    strategy: str
    credentials: dict[str, str] | None  # None means "use the ambient runtime role"
    fallback: bool = False
    fallback_reason: str | None = None
    token: TokenFacts = field(default_factory=TokenFacts)

    @property
    def role_name(self) -> str:
        return self.role_arn.rsplit("/", 1)[-1]

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "tier": self.tier,
            "resolved_role": self.role_name,
            "resolved_role_arn": self.role_arn,
            "strategy": self.strategy,
            "fallback": self.fallback,
            "token": self.token.as_dict(),
        }
        if self.fallback_reason:
            payload["fallback_reason"] = self.fallback_reason
        return payload


def _as_list(value: Any) -> list[str]:
    """Claims that hold one value may arrive as a bare string rather than a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def tier_from_groups(groups: Any) -> tuple[str, str]:
    """Map Okta groups to a tier. Least privilege wins, so a caller who is somehow in
    both groups resolves to business - resolution can never widen access."""
    found = {g.strip().lower() for g in _as_list(groups)}
    if FALLBACK_TIER in found:
        return FALLBACK_TIER, "both_groups" if "analytics" in found else "business_group"
    if "analytics" in found:
        return "analytics", "analytics_group"
    return FALLBACK_TIER, "no_recognised_group"


def _decode_payload(token: str) -> dict[str, Any] | None:
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment))
    except Exception:
        return None


def inspect_token(token: str | None, cfg: Config) -> tuple[dict[str, Any] | None, TokenFacts]:
    facts = TokenFacts()
    if not token:
        return None, facts
    facts.present = True
    claims = _decode_payload(token)
    if claims is None:
        return None, facts

    facts.token_use = claims.get("token_use")
    facts.claim_names = list(claims.keys())
    facts.subject_present = bool(claims.get("sub"))
    facts.issuer = claims.get("iss")
    facts.groups = _as_list(claims.get(cfg.okta_groups_claim))
    facts.issuer_matches = claims.get("iss") == cfg.okta_issuer
    audiences = _as_list(claims.get("aud")) + _as_list(claims.get("client_id"))
    facts.audience_matches = cfg.okta_audience in audiences
    exp = claims.get("exp")
    facts.expired = bool(exp and time.time() >= float(exp))
    return claims, facts


def _sts_assume(cfg: Config, role_arn: str, session_name: str) -> dict[str, str]:
    sts = boto3.client("sts", region_name=cfg.region)
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name[:64])["Credentials"]
    return {
        "aws_access_key_id": creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token": creds["SessionToken"],
    }


def _via_okta_groups(
    cfg: Config, claims: dict[str, Any], facts: TokenFacts
) -> ResolvedIdentity:
    """Resolve the tier from an Okta token's groups claim, then assume the tier role.

    This is the on-behalf-of path. The token arriving here is the *exchanged* token the
    Gateway obtained from Okta via RFC 8693: it still names the end user in "sub" while
    naming the exchanging service in "cid", and it carries the user's group. Because the
    group rides on the token, the server needs no directory lookup at all.

    Verified against the live authorization server - the groups claim survives the
    exchange, which is the fact this whole strategy depends on.
    """
    if cfg.okta_audience:
        audiences = _as_list(claims.get("aud")) + _as_list(claims.get("client_id"))
        if cfg.okta_audience not in audiences:
            return _fallback(cfg, "okta_audience_mismatch", facts)

    tier, why = tier_from_groups(claims.get(cfg.okta_groups_claim))
    role_arn = cfg.role_arn_for(tier)
    if not role_arn:
        return _fallback(cfg, f"unknown_tier_from_groups:{tier}", facts)

    try:
        credentials = _sts_assume(cfg, role_arn, f"{RESOURCE_PREFIX}-okta-{tier}")
    except ClientError as exc:
        return _fallback(cfg, f"assume_role_failed:{exc.response['Error']['Code']}", facts)

    unmatched = why == "no_recognised_group"
    return ResolvedIdentity(
        tier=tier,
        role_arn=role_arn,
        strategy=f"okta_groups_claim:{why}",
        credentials=credentials,
        # A caller with no recognised group is still least privilege, but flag it so the
        # demo can say *why* they only see the business columns.
        fallback=unmatched,
        fallback_reason="no_recognised_okta_group" if unmatched else None,
        token=facts,
    )


def _fallback(cfg: Config, reason: str, facts: TokenFacts) -> ResolvedIdentity:
    role_arn = cfg.role_arn_for(FALLBACK_TIER) or cfg.business_role_arn
    credentials = None
    try:
        credentials = _sts_assume(cfg, role_arn, f"{RESOURCE_PREFIX}-fallback")
    except ClientError as exc:
        # Still return the identity so the caller sees why, rather than a bare 500.
        log.error("fallback assume_role failed: %s", exc.response["Error"]["Code"])
        reason = f"{reason}; assume_role failed: {exc.response['Error']['Code']}"
    return ResolvedIdentity(
        tier=FALLBACK_TIER,
        role_arn=role_arn,
        strategy="fallback",
        credentials=credentials,
        fallback=True,
        fallback_reason=reason,
        token=facts,
    )


def resolve(cfg: Config, bearer: str | None) -> ResolvedIdentity:
    """Never raises. Always returns an identity, worst case the business tier."""
    claims, facts = inspect_token(bearer, cfg)

    if not facts.present:
        return _fallback(cfg, "no_authorization_header", facts)
    if claims is None:
        return _fallback(cfg, "token_unparseable", facts)
    if facts.expired:
        return _fallback(cfg, "token_expired", facts)

    # Requires okta_issuer to be configured AND to match exactly, so a token from any
    # other issuer can never reach this branch and select the analytics role.
    if cfg.okta_issuer and claims.get("iss") == cfg.okta_issuer:
        return _via_okta_groups(cfg, claims, facts)

    return _fallback(cfg, "unrecognised_issuer", facts)
