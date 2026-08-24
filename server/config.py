"""Server configuration, read once at startup from the environment.

Fails loudly and names every missing variable at once, so a bad deployment is fixed in
one pass instead of one restart per variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

# The catalog, the workgroup and the two tier role ARNs are the only variables every
# deployment must supply. Everything else has a working default.
REQUIRED = (
    "GLUE_DATABASE",
    "ATHENA_WORKGROUP",
    "ANALYTICS_ROLE_ARN",
    "BUSINESS_ROLE_ARN",
)


class MissingConfiguration(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    region: str
    glue_database: str
    athena_workgroup: str
    analytics_role_arn: str
    business_role_arn: str
    analytics_model_ids: tuple[str, ...] = ()
    business_model_ids: tuple[str, ...] = ()
    athena_timeout_seconds: float = 60.0
    athena_max_rows: int = 200
    tier_roles: Mapping[str, str] = field(default_factory=dict)
    # An Okta token is only honoured when okta_issuer is set AND matches, which keeps a
    # foreign token from ever selecting a tier.
    okta_issuer: str = ""
    okta_audience: str = ""
    okta_groups_claim: str = "groups"

    def role_arn_for(self, tier: str) -> str | None:
        return self.tier_roles.get(tier)

    def tier_for(self, role_arn: str) -> str | None:
        for tier, arn in self.tier_roles.items():
            if arn == role_arn:
                return tier
        return None

    def models_for(self, tier: str) -> tuple[str, ...]:
        return self.analytics_model_ids if tier == "analytics" else self.business_model_ids


# The least-privilege tier. An unidentified caller can only ever land here.
FALLBACK_TIER = "business"


def _csv(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in env.get(name, "").split(",") if v.strip())


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED if not env.get(name, "").strip()]
    if missing:
        raise MissingConfiguration(
            "missing required environment variables: " + ", ".join(missing)
        )

    # AgentCore reserves some AWS_* names, so SERVER_REGION (or the older
    # SERVER_REGION is the escape hatch.
    region = (
        env.get("SERVER_REGION")
        or env.get("AWS_REGION")
        or env.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    ).strip()

    analytics_arn = env["ANALYTICS_ROLE_ARN"].strip()
    business_arn = env["BUSINESS_ROLE_ARN"].strip()

    return Config(
        region=region,
        glue_database=env["GLUE_DATABASE"].strip(),
        athena_workgroup=env["ATHENA_WORKGROUP"].strip(),
        analytics_role_arn=analytics_arn,
        business_role_arn=business_arn,
        analytics_model_ids=_csv(env, "ANALYTICS_MODEL_IDS"),
        business_model_ids=_csv(env, "BUSINESS_MODEL_IDS"),
        athena_timeout_seconds=float(env.get("ATHENA_TIMEOUT_SECONDS", "60")),
        athena_max_rows=int(env.get("ATHENA_MAX_ROWS", "200")),
        tier_roles={"analytics": analytics_arn, "business": business_arn},
        okta_issuer=env.get("OKTA_ISSUER", "").strip().rstrip("/"),
        okta_audience=env.get("OKTA_AUDIENCE", "").strip(),
        okta_groups_claim=(env.get("OKTA_GROUPS_CLAIM") or "groups").strip(),
    )
