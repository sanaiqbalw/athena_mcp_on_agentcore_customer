"""Per-request AWS credentials for the upstream AWS Data Processing MCP server.

The upstream handlers build their boto3 clients once, in __init__, from the ambient role:

    self.athena_client = AwsHelper.create_boto3_client('athena')

That is fine for a single-user stdio server and wrong for a multi-tenant one. Rather than
fork the upstream code, we patch that one factory to hand back a proxy. The proxy holds no
client of its own; on every attribute access it resolves the client for whichever request
is currently executing. The upstream code is untouched and every one of its Glue and
Athena calls silently runs as the calling user's tier role.

Credentials live in a ContextVar, so concurrent requests cannot see each other's - each
asyncio task gets its own binding.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import Any, Callable

import boto3

from config import Config
from identity import ResolvedIdentity, resolve

log = logging.getLogger(f"{os.environ.get('RESOURCE_PREFIX') or 'mcp'}.aws_context")

_current: ContextVar["RequestScope | None"] = ContextVar("request_scope", default=None)


class RequestScope:
    """Everything tied to one inbound HTTP request."""

    def __init__(self, cfg: Config, bearer: str | None, header_names: list[str]):
        self._cfg = cfg
        self._bearer = bearer
        self._identity: ResolvedIdentity | None = None
        self._clients: dict[str, Any] = {}
        self._session: boto3.Session | None = None
        self.header_names = header_names

    @property
    def identity(self) -> ResolvedIdentity:
        # Resolved lazily and once: an MCP initialize or tools/list needs no AWS call.
        if self._identity is None:
            self._identity = resolve(self._cfg, self._bearer)
            log.info(
                "resolved tier=%s role=%s strategy=%s fallback=%s",
                self._identity.tier,
                self._identity.role_name,
                self._identity.strategy,
                self._identity.fallback,
            )
        return self._identity

    @property
    def session(self) -> boto3.Session:
        if self._session is None:
            creds = self.identity.credentials
            if creds:
                self._session = boto3.Session(region_name=self._cfg.region, **creds)
            else:
                # No vended credentials: fall back to the runtime's own role. Lake
                # Formation grants nothing to it, so queries fail closed rather than
                # returning data the caller should not see.
                self._session = boto3.Session(region_name=self._cfg.region)
        return self._session

    def client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = self.session.client(service, region_name=self._cfg.region)
        return self._clients[service]


class ClientProxy:
    """Stands in for a boto3 client, resolving the real one per request."""

    __slots__ = ("_service", "_fallback_factory")

    def __init__(self, service: str, fallback_factory: Callable[[str], Any]):
        self._service = service
        self._fallback_factory = fallback_factory

    def _target(self) -> Any:
        scope = _current.get()
        if scope is None:
            # Outside a request (startup, health checks). Use ambient credentials.
            return self._fallback_factory(self._service)
        return scope.client(self._service)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        scope = _current.get()
        where = "request" if scope else "ambient"
        return f"<ClientProxy {self._service} ({where})>"


def set_scope(scope: RequestScope):
    return _current.set(scope)


def reset_scope(token) -> None:
    _current.reset(token)


def current_scope() -> RequestScope | None:
    return _current.get()


def install(cfg: Config) -> None:
    """Patch the upstream client factory to return request-scoped proxies."""
    from awslabs.aws_dataprocessing_mcp_server.utils.aws_helper import AwsHelper

    original = AwsHelper.create_boto3_client.__func__  # unbound classmethod

    def _ambient(service: str) -> Any:
        return original(AwsHelper, service, cfg.region)

    def create_boto3_client(cls, service_name: str, region_name: str | None = None) -> Any:
        return ClientProxy(service_name, _ambient)

    AwsHelper.create_boto3_client = classmethod(create_boto3_client)
    log.info("patched AwsHelper.create_boto3_client with request-scoped proxies")
