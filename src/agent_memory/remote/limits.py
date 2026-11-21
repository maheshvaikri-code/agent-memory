"""The guard in front of the MCP app: who is calling, how much they may send, how many wrong
guesses are tolerated. Pure ASGI; imports nothing from `mcp`."""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from collections import deque
from typing import Callable, Deque, List, Optional, Tuple

from .tokens import Principal, TokenFile

MAX_BODY_BYTES = 1_000_000
MAX_FAILURES = 20
FAILURE_WINDOW = 60.0
MAX_CREDENTIAL_CHARS = 512      # a longer bearer value is refused before it is hashed

# One line per event: names, tool names, outcome codes and durations; never an argument, a
# result or a token.
audit_log = logging.getLogger("agent_memory.remote.audit")

current_principal: "contextvars.ContextVar[Optional[Principal]]" = contextvars.ContextVar(
    "agent_memory_principal", default=None)


class FailureLimiter:
    """Counts failed authentications in a sliding window. Global, not per address: behind a
    proxy every request shares an address."""

    def __init__(self, max_failures: int = MAX_FAILURES, window: float = FAILURE_WINDOW,
                 clock: Callable[[], float] = time.monotonic):
        self._max, self._window, self._clock = max_failures, window, clock
        self._failures: Deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self) -> None:
        horizon = self._clock() - self._window
        while self._failures and self._failures[0] <= horizon:
            self._failures.popleft()

    def blocked(self) -> bool:
        with self._lock:
            self._trim()
            return len(self._failures) >= self._max

    def record(self) -> None:
        with self._lock:
            self._trim()
            self._failures.append(self._clock())


def _bearer(headers: List[Tuple[bytes, bytes]]) -> Tuple[Optional[str], str]:
    """(token, reason): reason is "" when a single well-formed bearer credential was sent."""
    values = [value for name, value in headers if name == b"authorization"]
    if not values:
        return None, "missing"
    if len(values) > 1:
        return None, "invalid"
    parts = values[0].decode("latin-1").split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None, "invalid"
    if len(parts[1]) > MAX_CREDENTIAL_CHARS:
        return None, "invalid"
    return parts[1], ""


async def _respond(send, status: int, body: dict, extra: Optional[List[Tuple[bytes, bytes]]] = None):
    data = json.dumps(body).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(data)).encode())] + list(extra or [])})
    await send({"type": "http.response.body", "body": data})


class GuardMiddleware:
    def __init__(self, app, tokens: TokenFile, limiter: Optional[FailureLimiter] = None,
                 max_body: int = MAX_BODY_BYTES):
        self.app, self.tokens, self.max_body = app, tokens, max_body
        self.limiter = limiter if limiter is not None else FailureLimiter()

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":                            # the only scope passed unauthenticated
            await self.app(scope, receive, send)
            return
        if kind == "websocket":                           # no websocket is served: refuse the handshake
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http":                                # anything unknown is refused, not passed on
            return
        token, reason = _bearer(scope["headers"])
        principal = self.tokens.verify(token) if token else None
        if principal is None:
            reason = reason or "invalid"
            if self.limiter.blocked():
                audit_log.warning("auth denied reason=%s throttled=yes", reason)
                await _respond(send, 429, {"error": "too_many_failures"}, [(b"retry-after", b"60")])
                return
            self.limiter.record()
            audit_log.warning("auth denied reason=%s", reason)
            await _respond(send, 401, {"error": "unauthorized"}, [(b"www-authenticate", b"Bearer")])
            return
        body = await self._read_body(scope, receive, send)
        if body is None:
            return
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()                        # a later disconnect

        marker = current_principal.set(principal)
        try:
            await self.app(scope, replay, send)
        finally:
            current_principal.reset(marker)

    async def _read_body(self, scope, receive, send) -> Optional[bytes]:
        """The whole request body, or None after answering 413 or 400."""
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_body:
                    await _respond(send, 413, {"error": "payload_too_large"})
                    return None
            except ValueError:
                await _respond(send, 400, {"error": "bad_request"})
                return None
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                await _respond(send, 413, {"error": "payload_too_large"})
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)
