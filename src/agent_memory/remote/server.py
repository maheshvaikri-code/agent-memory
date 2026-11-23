"""The HTTP server: one MemoryService per token over a single store, behind the guard.
Imports `mcp`; load it lazily."""
from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import sys
import threading
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple
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


DRAIN_SECONDS = 15.0


class _Entry:
    """A cached service, how many calls hold it open, and whether its token has gone."""
    __slots__ = ("service", "leases", "retired")

    def __init__(self, service: MemoryService):
        self.service, self.leases, self.retired = service, 0, False


class Principals:
    """A lazily opened MemoryService per (name, role, creation time) of a token. Each has the
    token's name as its agent, `allow_must` off and paths redacted. A call holds a lease on its
    service for its whole duration. A token that left the file has its service closed at the next
    call, or, if a call is still using it, when that call's lease ends."""

    def __init__(self, base: ServiceConfig, tokens: TokenFile):
        self._base, self._tokens = base, tokens
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)    # signalled when a retired service is released
        self._services: Dict[Tuple[str, str, str], _Entry] = {}
        self._retired: List[_Entry] = []                   # departed tokens' services still in use
        self._generation = -1
        self._closed = False

    @property
    def open_count(self) -> int:
        with self._lock:
            return len(self._services)

    @property
    def retired_count(self) -> int:
        with self._lock:
            return len(self._retired)

    @property
    def leases_held(self) -> int:
        with self._lock:
            return sum(e.leases for e in self._services.values()) + \
                sum(e.leases for e in self._retired)

    def _entry_for(self, principal: Principal) -> _Entry:
        """The caller holds the lock."""
        if self._closed:
            raise ServiceError("internal", "the server is shutting down")
        self._sweep()
        key = (principal.name, principal.role, principal.created_at)
        entry = self._services.get(key)
        if entry is None:
            entry = _Entry(MemoryService(replace(self._base, agent=principal.name,
                                                 allow_must=False, read_only=False,
                                                 redact_paths=True)))
            self._services[key] = entry
        return entry

    @staticmethod
    def _view(principal: Principal, service: MemoryService):
        return ReadOnlyView(service) if principal.role == "read-only" else service

    def service_for(self, principal: Principal):
        """The service (or read-only view) without holding it open. Tests and callers that do not
        run a call use this; a call uses `lease`."""
        with self._lock:
            entry = self._entry_for(principal)
        return self._view(principal, entry.service)

    @contextlib.contextmanager
    def lease(self, principal: Principal):
        """Hold the principal's service open for the duration of the `with` block; yields
        (service or read-only view, role). Released on success, error or cancellation."""
        with self._lock:
            entry = self._entry_for(principal)
            entry.leases += 1
        try:
            yield self._view(principal, entry.service), principal.role
        finally:
            self._release(entry)

    def _release(self, entry: _Entry) -> None:
        with self._lock:
            entry.leases -= 1
            last = entry.retired and entry.leases == 0 and entry in self._retired
        if not last:
            return
        self._close_quietly(entry.service)                 # outside the lock: a close can take a moment
        with self._changed:
            if entry in self._retired:
                self._retired.remove(entry)
            self._changed.notify_all()

    def _sweep(self) -> None:
        """Close the services of tokens that left the file, but only on a consistent, clean
        snapshot: no error before the read and the same generation around it (every refresh
        that changes anything bumps it, so the entries came from one clean read). Otherwise do
        nothing and do not record the generation, so the next call retries. An outage lists no
        tokens; closing then would lose every session (requests still fail closed at the guard).
        A departed service with a call running is retired, not closed: its last lease closes it."""
        before = self._tokens.generation
        if self._tokens.error:
            return
        entries = self._tokens.entries()
        if self._tokens.generation != before or before == self._generation:
            return                         # the file changed or failed meanwhile, or nothing new
        self._generation = before
        live = {(e.name, e.role, e.created_at) for e in entries}
        for key in [k for k in self._services if k not in live]:
            entry = self._services.pop(key)
            if entry.leases == 0:
                self._close_quietly(entry.service)
            else:
                entry.retired = True
                self._retired.append(entry)

    @staticmethod
    def _close_quietly(service: MemoryService) -> None:
        try:
            service.close()
        except Exception:
            logging.getLogger(__name__).exception("a service could not be closed cleanly")

    def close(self, drain_seconds: float = DRAIN_SECONDS) -> None:
        """Refuse new leases, close every idle service, wait up to `drain_seconds` for running
        calls to finish (their last lease closes their service), then close what is left and
        say so. Closing a service still takes its own lock, so a forced close waits for a call
        inside a method, which the store's lock timeout bounds. Idempotent."""
        with self._lock:
            self._closed = True
            for entry in self._services.values():
                entry.retired = True
                self._retired.append(entry)
            self._services = {}
            idle = [e for e in self._retired if e.leases == 0]
            self._retired = [e for e in self._retired if e.leases > 0]
        for entry in idle:
            self._close_quietly(entry.service)
        deadline = time.monotonic() + drain_seconds
        with self._changed:
            while self._retired:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            stuck, self._retired = self._retired, []
        if stuck:
            logging.getLogger(__name__).warning(
                "closing %d service(s) with calls still running", len(stuck))
        for entry in stuck:
            self._close_quietly(entry.service)


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


def public_hosts(public_url: str) -> List[str]:
    """The Host values `--public-url` allows: the lower-cased host with and without a port, and the
    exact port when the URL names one. An empty URL allows nothing extra; a URL that is not
    http(s)://host[:port] is refused, since it would otherwise be silently ineffective."""
    if not public_url:
        return []
    problem = StartupError("--public-url must look like https://host[:port]")
    try:
        parsed = urlparse(public_url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise problem from None
    if parsed.scheme not in ("http", "https") or not host:
        raise problem
    if ":" in host:
        host = f"[{host}]"                                 # an IPv6 literal, as a Host header writes it
    hosts = [host, host + ":*"]
    if port is not None:
        hosts.append(f"{host}:{port}")
    return hosts


def build_app(base: ServiceConfig, tokens: TokenFile, *, public_url: str = "",
              limiter: Optional[FailureLimiter] = None):
    """(asgi_app, principals). The app authenticates every request before anything else."""
    principals = Principals(base, tokens)
    hosts = LOOPBACK_HOSTS + public_hosts(public_url)

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


def check_startup(base: ServiceConfig, tokens: TokenFile, host: str, behind_tls: bool,
                  public_url: str = "") -> None:
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
    public_hosts(public_url)                           # refuses a URL that would silently not work
    if not os.path.isdir(base.repo_root):
        raise StartupError(f"the repo root {base.repo_root} is not a directory")
    try:                                               # the first request would open the store: do it now
        MemoryService(replace(base, agent="startup-check", allow_must=False, read_only=False,
                              redact_paths=True)).close()
    except (ServiceError, OSError) as exc:
        detail = f"{type(exc).__name__}: {exc}" if isinstance(exc, OSError) else f"{exc.code}: {exc}"
        raise StartupError(f"the memory store is unusable ({detail[:300]}); fix or remove it, "
                           "or point --store/--repo elsewhere") from None


def startup_banner(host: str, port: int, token_path, usable: int, hosts: List[str]) -> str:
    shown = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return (f"agent-memory remote: listening on http://{shown}:{port}/mcp, token file {token_path}, "
            f"{usable} usable token(s), allowed hosts: {', '.join(hosts)}")


def _audit_formatter() -> logging.Formatter:
    formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
    formatter.converter = time.gmtime                  # UTC, and the Z says so
    return formatter


def _configure_audit_log() -> None:
    if audit_log.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_audit_formatter())
    audit_log.addHandler(handler)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False
    logging.getLogger("mcp").setLevel(logging.WARNING)


def _say(line: str) -> None:
    """One line to stderr that never fails on a character the stream cannot encode."""
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    print(line.encode(encoding, errors="replace").decode(encoding, errors="replace"),
          file=sys.stderr, flush=True)


def run_http(base: ServiceConfig, tokens: TokenFile, host: str, port: int, public_url: str = "",
             behind_tls: bool = False) -> None:
    import uvicorn

    check_startup(base, tokens, host, behind_tls, public_url)
    _configure_audit_log()
    app, principals = build_app(base, tokens, public_url=public_url)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                           access_log=False, server_header=False))
    _say(startup_banner(host, port, tokens.path, tokens.usable(),
                        LOOPBACK_HOSTS + public_hosts(public_url)))
    try:
        server.run()
    finally:
        principals.close()                              # each service leaves a checkpoint
