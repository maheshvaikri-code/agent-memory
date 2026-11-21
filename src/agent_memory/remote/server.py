"""The HTTP server: one MemoryService per token over a single store, behind the guard.
Imports `mcp`; load it lazily."""
from __future__ import annotations

import ipaddress
import logging
import os
import sys
import threading
from dataclasses import replace
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings

from ..mcp_server import build_server
from ..service import MemoryService, ServiceConfig, ServiceError, path_scrubber
from .limits import FailureLimiter, GuardMiddleware, audit_log, current_principal
from .tokens import Principal, TokenFile

LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]


class StartupError(Exception):
    """The server will not start; the message says why and what to do."""


class ReadOnlyView:
    """What a read-only token can reach: three methods and nothing else."""

    def __init__(self, service: MemoryService):
        self._service = service

    def recall(self, **kwargs: Any) -> Dict[str, Any]:
        return self._service.recall(**kwargs)

    def check(self, **kwargs: Any) -> Dict[str, Any]:
        return self._service.check(**kwargs)

    def status(self) -> Dict[str, Any]:
        return self._service.status()


class Principals:
    """A lazily opened MemoryService per (name, role, creation time) of a token. Each has the
    token's name as its agent, `allow_must` off and paths redacted. A token that left the file
    has its service closed at the next call."""

    def __init__(self, base: ServiceConfig, tokens: TokenFile):
        self._base, self._tokens = base, tokens
        self._lock = threading.Lock()
        self._services: Dict[Tuple[str, str, str], MemoryService] = {}
        self._generation = -1
        self._closed = False

    @property
    def open_count(self) -> int:
        with self._lock:
            return len(self._services)

    def service_for(self, principal: Principal):
        with self._lock:
            if self._closed:
                raise ServiceError("internal", "the server is shutting down")
            self._sweep()
            key = (principal.name, principal.role, principal.created_at)
            service = self._services.get(key)
            if service is None:
                service = MemoryService(replace(self._base, agent=principal.name,
                                                allow_must=False, read_only=False,
                                                redact_paths=True))
                self._services[key] = service
        return ReadOnlyView(service) if principal.role == "read-only" else service

    def _sweep(self) -> None:
        """Close the services of tokens that left the file, but only on a consistent, clean
        snapshot: no error before the read and the same generation around it (every refresh
        that changes anything bumps it, so the entries came from one clean read). Otherwise do
        nothing and do not record the generation, so the next call retries. An outage lists no
        tokens; closing then would lose every session (requests still fail closed at the guard)."""
        before = self._tokens.generation
        if self._tokens.error:
            return
        entries = self._tokens.entries()
        if self._tokens.generation != before or before == self._generation:
            return                         # the file changed or failed meanwhile, or nothing new
        self._generation = before
        live = {(e.name, e.role, e.created_at) for e in entries}
        for key in [k for k in self._services if k not in live]:
            self._close_quietly(self._services.pop(key))

    @staticmethod
    def _close_quietly(service: MemoryService) -> None:
        try:
            service.close()
        except Exception:
            logging.getLogger(__name__).exception("a service could not be closed cleanly")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            services, self._services = list(self._services.values()), {}
        for service in services:
            self._close_quietly(service)


class CloseOnShutdown:
    """Passes every scope through unchanged; for the lifespan scope it closes the per-token
    services when the inner app reports its shutdown, before the server goes on to exit. uvicorn
    re-raises the stop signal once it has shut down, which can end the process before any code
    after `server.run()`; this runs inside the shutdown, so each service still leaves a checkpoint."""

    def __init__(self, app, principals: "Principals"):
        self.app, self.principals = app, principals

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        async def watching(message):
            if message["type"] in ("lifespan.shutdown.complete", "lifespan.shutdown.failed"):
                self.principals.close()                    # idempotent; the server is stopping anyway
            await send(message)

        await self.app(scope, receive, watching)


def build_app(base: ServiceConfig, tokens: TokenFile, *, public_url: str = "",
              limiter: Optional[FailureLimiter] = None):
    """(asgi_app, principals). The app authenticates every request before anything else."""
    principals = Principals(base, tokens)
    hosts = list(LOOPBACK_HOSTS)
    public_host = urlparse(public_url).netloc if public_url else ""
    if public_host:
        hosts += [public_host] + ([] if ":" in public_host else [public_host + ":*"])

    def provider():
        principal = current_principal.get()
        if principal is None:                          # the guard always sets it: fail closed
            raise ServiceError("forbidden", "no authenticated caller")
        return principals.service_for(principal), principal.role

    def observer(tool: str, outcome: str, ms: int) -> None:
        principal = current_principal.get()
        audit_log.info("call name=%s tool=%s outcome=%s ms=%d",
                       principal.name if principal else "-", tool, outcome, ms)

    server = build_server(
        provider=provider, trust_client_name=False, observer=observer, scrub=path_scrubber(base),
        settings={"stateless_http": True, "json_response": True,
                  "transport_security": TransportSecuritySettings(
                      enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                      allowed_origins=[])})
    guarded = GuardMiddleware(server.streamable_http_app(), tokens, limiter)
    return CloseOnShutdown(guarded, principals), principals


def is_loopback(host: str) -> bool:
    name = (host or "").strip().strip("[]")
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def check_startup(base: ServiceConfig, tokens: TokenFile, host: str, behind_tls: bool) -> None:
    if not is_loopback(host) and not behind_tls:
        raise StartupError(
            f"binding {host or 'an empty host'} exposes a bearer token over plain HTTP. Put a TLS "
            "proxy or tunnel (Caddy, nginx, Cloudflare Tunnel) in front and add --behind-tls to "
            "say so, or bind 127.0.0.1")
    if tokens.error:
        raise StartupError(f"the token file {tokens.path} is unusable: {tokens.error}")
    if tokens.usable() == 0:
        raise StartupError(f"no usable token in {tokens.path}; run `amem token create NAME "
                           "--role read-write` first (there is no anonymous mode)")
    if not os.path.isdir(base.repo_root):
        raise StartupError(f"the repo root {base.repo_root} is not a directory")


def _configure_audit_log() -> None:
    if audit_log.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    audit_log.addHandler(handler)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False
    logging.getLogger("mcp").setLevel(logging.WARNING)


def run_http(base: ServiceConfig, tokens: TokenFile, host: str, port: int, public_url: str = "",
             behind_tls: bool = False) -> None:
    import uvicorn

    check_startup(base, tokens, host, behind_tls)
    _configure_audit_log()
    app, principals = build_app(base, tokens, public_url=public_url)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                           access_log=False))
    try:
        server.run()
    finally:
        principals.close()                              # each service leaves a checkpoint
