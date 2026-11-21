import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("starlette")

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_memory.remote.limits import (MAX_CREDENTIAL_CHARS, FailureLimiter, GuardMiddleware,
                                        current_principal)
from agent_memory.remote.tokens import TokenFile

UNAUTHORIZED = {"error": "unauthorized"}


async def echo(request):
    body = await request.body()
    principal = current_principal.get()
    return JSONResponse({"who": principal.name if principal else None,
                         "role": principal.role if principal else None, "bytes": len(body)})


def inner():
    return Starlette(routes=[Route("/mcp", echo, methods=["GET", "POST", "DELETE"])])


@pytest.fixture
def tokens(tmp_path):
    return TokenFile(tmp_path / "tokens.json")


@pytest.fixture
def client(tokens):
    return TestClient(GuardMiddleware(inner(), tokens, max_body=100))


def bearer(token, scheme="Bearer"):
    return {"Authorization": f"{scheme} {token}"}


def assert_denied(response):
    assert response.status_code == 401 and response.json() == UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"


def test_no_credential_is_401_with_a_fixed_body(client, tokens):
    tokens.create("alice", "read-write")
    assert_denied(client.post("/mcp", json={}))


def test_a_wrong_token_looks_exactly_like_no_token(client, tokens):
    tokens.create("alice", "read-write")
    missing = client.post("/mcp", json={})
    wrong = client.post("/mcp", json={}, headers=bearer("amt_" + "A" * 43))
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json() and wrong.headers["www-authenticate"] == "Bearer"


def test_a_good_token_reaches_the_app_with_its_identity(client, tokens):
    token = tokens.create("alice", "read-only")
    response = client.post("/mcp", json={"a": 1}, headers=bearer(token))
    assert response.status_code == 200 and response.json()["who"] == "alice"
    assert response.json()["role"] == "read-only"


def test_the_scheme_is_case_insensitive(client, tokens):
    token = tokens.create("alice", "read-write")
    assert client.post("/mcp", json={}, headers=bearer(token, "bearer")).status_code == 200
    assert client.post("/mcp", json={}, headers=bearer(token, "BEARER")).status_code == 200


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Basic abc", "Bearer a b", "token amt_x",
                                    "Bearer  amt_x"])
def test_malformed_credentials_are_401(client, tokens, header):
    tokens.create("alice", "read-write")
    assert_denied(client.post("/mcp", json={}, headers={"Authorization": header}))


def test_two_authorization_headers_are_refused_even_when_one_is_valid(client, tokens):
    token = tokens.create("alice", "read-write")
    response = client.post("/mcp", json={}, headers=[("Authorization", f"Bearer {token}"),
                                                     ("Authorization", f"Bearer {token}")])
    assert_denied(response)


def test_a_revoked_token_is_refused_on_the_next_request(client, tokens):
    token = tokens.create("alice", "read-write")
    assert client.post("/mcp", json={}, headers=bearer(token)).status_code == 200
    tokens.revoke("alice")
    assert_denied(client.post("/mcp", json={}, headers=bearer(token)))


def test_an_expired_token_is_refused(tmp_path):
    tokens = TokenFile(tmp_path / "t.json")
    token = tokens.create("alice", "read-write", expires_in_days=1,
                          now=datetime.now(timezone.utc) - timedelta(days=3))
    client = TestClient(GuardMiddleware(inner(), tokens))
    assert_denied(client.post("/mcp", json={}, headers=bearer(token)))


def test_unknown_paths_are_not_probeable_without_a_token(client):
    assert_denied(client.get("/anything"))


def test_a_body_over_the_cap_is_413_by_content_length(client, tokens):
    token = tokens.create("alice", "read-write")
    response = client.post("/mcp", content=b"x" * 101, headers=bearer(token))
    assert response.status_code == 413 and response.json() == {"error": "payload_too_large"}


def test_a_chunked_body_over_the_cap_is_413(client, tokens):
    token = tokens.create("alice", "read-write")
    response = client.post("/mcp", content=iter([b"x" * 60, b"x" * 60]), headers=bearer(token))
    assert response.status_code == 413


def test_a_body_at_the_cap_passes_whole(client, tokens):
    token = tokens.create("alice", "read-write")
    response = client.post("/mcp", content=b"x" * 100, headers=bearer(token))
    assert response.status_code == 200 and response.json()["bytes"] == 100


def test_an_oversized_body_without_a_token_is_401_not_413(client):
    assert_denied(client.post("/mcp", content=b"x" * 500))


def test_the_limiter_blocks_after_too_many_failures_but_never_a_valid_token(tokens):
    token = tokens.create("alice", "read-write")
    client = TestClient(GuardMiddleware(inner(), tokens, FailureLimiter(3, 60.0)))
    for _ in range(3):
        assert client.post("/mcp", json={}, headers=bearer("amt_bad")).status_code == 401
    blocked = client.post("/mcp", json={}, headers=bearer("amt_bad"))
    assert blocked.status_code == 429 and blocked.json() == {"error": "too_many_failures"}
    assert blocked.headers["retry-after"] == "60"
    assert client.post("/mcp", json={}).status_code == 429
    assert client.post("/mcp", json={}, headers=bearer(token)).status_code == 200


def test_the_limiter_forgets_failures_after_its_window():
    now = [0.0]
    limiter = FailureLimiter(2, 60.0, clock=lambda: now[0])
    limiter.record()
    limiter.record()
    assert limiter.blocked()
    now[0] = 61.0
    assert not limiter.blocked()


def test_the_lifespan_passes_through(tokens):
    started = []

    @contextlib.asynccontextmanager
    async def lifespan(app):
        started.append("up")
        yield
        started.append("down")

    app = Starlette(routes=[Route("/mcp", echo)], lifespan=lifespan)
    with TestClient(GuardMiddleware(app, tokens)):
        assert started == ["up"]
    assert started == ["up", "down"]


def test_denials_are_audited_without_the_presented_value(client, tokens, caplog):
    tokens.create("alice", "read-write")
    secret = "amt_" + "S" * 43
    with caplog.at_level(logging.INFO, logger="agent_memory.remote.audit"):
        client.post("/mcp", json={}, headers=bearer(secret))
        client.post("/mcp", json={})
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "reason=invalid" in text and "reason=missing" in text and secret not in text


def test_the_principal_is_not_left_set_after_a_request(tokens):
    token = tokens.create("alice", "read-write")
    seen = []

    async def app(scope, receive, send):
        seen.append(current_principal.get().name)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    guard = GuardMiddleware(app, tokens)

    async def go():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            pass

        scope = {"type": "http", "method": "POST",
                 "headers": [(b"authorization", f"Bearer {token}".encode())]}
        await guard(scope, receive, send)
        return current_principal.get()

    assert asyncio.run(go()) is None and seen == ["alice"]


def test_an_oversized_credential_is_refused_without_being_verified(client, tokens, monkeypatch):
    tokens.create("alice", "read-write")
    calls = []
    real = tokens.verify
    monkeypatch.setattr(tokens, "verify", lambda token: calls.append(token) or real(token))
    assert_denied(client.post("/mcp", json={}, headers=bearer("A" * (MAX_CREDENTIAL_CHARS + 88))))
    assert calls == []
    assert_denied(client.post("/mcp", json={}, headers=bearer("A" * MAX_CREDENTIAL_CHARS)))
    assert len(calls) == 1


def test_an_oversized_credential_is_audited_as_invalid(client, tokens, caplog):
    secret = "A" * 600
    with caplog.at_level(logging.INFO, logger="agent_memory.remote.audit"):
        client.post("/mcp", json={}, headers=bearer(secret))
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "reason=invalid" in text and secret not in text


def _drive(guard, scope, incoming):
    """Run the guard on a raw scope with a scripted receive; return what it sent."""
    sent = []
    queue = list(incoming)

    async def receive():
        return queue.pop(0) if queue else {"type": "websocket.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(guard(scope, receive, send))
    return sent


def _recording_app():
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["type"])

    return app, calls


def test_a_websocket_is_closed_and_never_reaches_the_app(tokens):
    app, calls = _recording_app()
    token = tokens.create("alice", "read-write")
    scope = {"type": "websocket", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    sent = _drive(GuardMiddleware(app, tokens), scope, [{"type": "websocket.connect"}])
    assert calls == [] and sent == [{"type": "websocket.close", "code": 1008}]


def test_an_unknown_scope_type_never_reaches_the_app(tokens):
    app, calls = _recording_app()
    sent = _drive(GuardMiddleware(app, tokens), {"type": "future", "headers": []}, [])
    assert calls == [] and sent == []


def test_the_lifespan_scope_reaches_the_app_directly(tokens):
    app, calls = _recording_app()
    _drive(GuardMiddleware(app, tokens), {"type": "lifespan"}, [])
    assert calls == ["lifespan"]


def _chunked(guard_tokens, token, chunks, max_body=100):
    """Drive the guard with a valid token and a body split over http.request messages."""
    app_calls = []

    async def app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        app_calls.append(len(body))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(b"authorization", f"Bearer {token}".encode())]}
    incoming = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
                for index, chunk in enumerate(chunks)]
    sent = _drive(GuardMiddleware(app, guard_tokens, max_body=max_body), scope, incoming)
    return sent, app_calls


def test_the_cap_counts_a_body_across_chunks(tokens):
    token = tokens.create("alice", "read-write")
    sent, calls = _chunked(tokens, token, [b"a" * 60, b"b" * 60])
    assert sent[0]["status"] == 413 and calls == []
    assert json.loads(sent[1]["body"]) == {"error": "payload_too_large"}


def test_a_body_of_exactly_the_cap_across_chunks_passes_whole(tokens):
    token = tokens.create("alice", "read-write")
    sent, calls = _chunked(tokens, token, [b"a" * 60, b"b" * 40])
    assert sent[0]["status"] == 200 and calls == [100]


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"])
def test_after_authentication_only_post_is_served(tokens, method):
    token = tokens.create("alice", "read-write")
    app, calls = _recording_app()
    scope = {"type": "http", "method": method, "path": "/mcp",
             "headers": [(b"authorization", f"Bearer {token}".encode())]}
    sent = _drive(GuardMiddleware(app, tokens), scope, [])      # no receive() may be needed
    assert calls == []
    assert sent[0]["status"] == 405
    assert (b"allow", b"POST") in sent[0]["headers"]
    assert json.loads(sent[1]["body"]) == {"error": "method_not_allowed"}


def test_a_405_applies_to_any_path_and_unauthenticated_requests_still_get_401(tokens):
    token = tokens.create("alice", "read-write")
    app, calls = _recording_app()
    guard = GuardMiddleware(app, tokens)
    other = _drive(guard, {"type": "http", "method": "GET", "path": "/elsewhere",
                           "headers": [(b"authorization", f"Bearer {token}".encode())]}, [])
    assert other[0]["status"] == 405
    for method in ("GET", "PUT", "DELETE", "HEAD", "OPTIONS"):
        sent = _drive(guard, {"type": "http", "method": method, "path": "/mcp", "headers": []}, [])
        assert sent[0]["status"] == 401
    assert calls == []


def test_a_valid_post_is_unchanged(client, tokens):
    token = tokens.create("alice", "read-write")
    assert client.post("/mcp", json={}, headers=bearer(token)).status_code == 200
    assert client.get("/mcp", headers=bearer(token)).status_code == 405
    assert client.get("/mcp", headers=bearer(token)).headers["allow"] == "POST"
    assert client.get("/mcp").status_code == 401
