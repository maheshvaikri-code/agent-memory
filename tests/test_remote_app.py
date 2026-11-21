import concurrent.futures
import json
import logging
import threading
from datetime import datetime, timezone

import pytest

pytest.importorskip("mcp")
from starlette.testclient import TestClient                          # noqa: E402

from agent_memory.remote.server import (Principals, ReadOnlyView, StartupError, build_app,  # noqa: E402
                                        check_startup, is_loopback)
from agent_memory.remote.limits import FailureLimiter                # noqa: E402
from agent_memory.remote.tokens import Principal, TokenFile          # noqa: E402
from agent_memory.service import resolve_config                      # noqa: E402

ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "work" / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def base(repo):
    return resolve_config(repo=str(repo), agent="remote", env={})


@pytest.fixture
def tokens(tmp_path):
    return TokenFile(tmp_path / "tokens.json")


@pytest.fixture
def app_client(base, tokens):
    app, principals = build_app(base, tokens, public_url="https://mem.example.com")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield client, principals
    principals.close()


def rpc(client, token, method, params=None, headers=None, call_id=1):
    sent = dict(ACCEPT)
    if token:
        sent["Authorization"] = f"Bearer {token}"
    sent.update(headers or {})
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": call_id, "method": method,
                                     "params": params or {}}, headers=sent)


def tool(client, token, name, **arguments):
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    text = result["content"][0]["text"]
    return result["isError"], (text if result["isError"] else json.loads(text))


INIT = {"protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "evil-bot", "version": "1"}}


def test_an_unauthenticated_initialize_is_401(app_client, tokens):
    client, _ = app_client
    tokens.create("alice", "read-write")
    assert rpc(client, None, "initialize", INIT).status_code == 401


def test_the_tool_list_is_the_stdio_surface(app_client, tokens):
    from agent_memory.mcp_server import TOOL_NAMES, build_server
    from agent_memory.service import MemoryService
    client, _ = app_client
    token = tokens.create("alice", "read-write")
    remote = rpc(client, token, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in remote] == list(TOOL_NAMES)
    assert not any(word in t["name"] for t in remote
                   for word in ("review", "accept", "reject", "promote"))
    local = build_server(MemoryService.__new__(MemoryService))          # descriptions only
    import asyncio
    stdio = asyncio.run(local.list_tools())
    assert [(t["name"], t["description"]) for t in remote] == [(t.name, t.description) for t in stdio]


def test_a_read_write_token_writes_and_a_read_only_token_reads_it(app_client, tokens):
    client, _ = app_client
    rw, ro = tokens.create("alice", "read-write"), tokens.create("bob", "read-only")
    is_error, made = tool(client, rw, "memory_remember", kind="quirk",
                          fields={"description": "the zorblax queue drops items over 64KB"},
                          files=["src/cache.py"])
    assert not is_error and made["created"] is True
    is_error, found = tool(client, ro, "memory_recall", task="zorblax queue drops items")
    assert not is_error and [i["id"] for i in found["items"]] == [made["id"]]


def test_a_read_only_token_cannot_write_and_nothing_changes(app_client, tokens):
    client, _ = app_client
    ro = tokens.create("bob", "read-only")
    is_error, text = tool(client, ro, "memory_remember", kind="quirk",
                          fields={"description": "should not be stored"})
    assert is_error and "forbidden" in text
    is_error, status = tool(client, ro, "memory_status")
    assert status["total_active"] == 0


def test_notes_carry_the_token_name_whatever_the_client_claims(app_client, tokens, base):
    client, principals = app_client
    token = tokens.create("base44-prod", "read-write")
    rpc(client, token, "initialize", INIT)                       # the client says it is "evil-bot"
    _, made = tool(client, token, "memory_remember", kind="quirk",
                   fields={"description": "who wrote this"})
    service = principals.service_for(Principal("base44-prod", "read-write",
                                               tokens.entries()[0].created_at))
    assert service.memory.graph.get_node("quirk", made["id"]).properties["agent"] == "base44-prod"


def test_two_tokens_keep_separate_sessions(app_client, tokens):
    client, _ = app_client
    a, b = tokens.create("alice", "read-write"), tokens.create("bob", "read-write")
    _, first = tool(client, a, "memory_session_start", task="task a")
    _, second = tool(client, b, "memory_session_start", task="task b")
    assert first["session_id"] != second["session_id"]
    _, again = tool(client, a, "memory_session_start", task="task a again")
    assert again["already_active"] is True and again["session_id"] == first["session_id"]


def test_a_lesson_captured_remotely_is_pending_and_not_recalled(app_client, tokens):
    client, _ = app_client
    rw = tokens.create("alice", "read-write")
    is_error, out = tool(client, rw, "memory_capture_lessons", lessons=[
        {"kind": "quirk", "fields": {"description": "the retry queue drops jobs on restart"},
         "evidence": "seen in worker.py", "files": ["src/cache.py"]}])
    assert not is_error and len(out["captured"]) == 1 and out["pending"] == 1
    _, found = tool(client, rw, "memory_recall", task="retry queue drops jobs on restart")
    assert found["items"] == []


def test_a_retried_write_stores_one_note(app_client, tokens):
    client, _ = app_client
    rw = tokens.create("alice", "read-write")
    args = dict(kind="quirk", fields={"description": "idempotent under retry"})
    _, first = tool(client, rw, "memory_remember", **args)
    _, second = tool(client, rw, "memory_remember", **args)
    assert first["id"] == second["id"] and second["merged"] is True
    _, status = tool(client, rw, "memory_status")
    assert status["total_active"] == 1


def test_priority_must_is_never_available_remotely(app_client, tokens):
    client, _ = app_client
    rw = tokens.create("alice", "read-write")
    is_error, text = tool(client, rw, "memory_remember", kind="quirk",
                          fields={"description": "pin me"}, priority="must")
    assert is_error and "allow_must" in text


def test_no_absolute_server_path_reaches_the_caller(app_client, tokens, tmp_path):
    client, _ = app_client
    rw = tokens.create("alice", "read-write")
    inside = str(tmp_path / "work" / "repo" / "src" / "cache.py")     # the server's own layout
    calls = [("memory_status", {}),
             ("memory_remember", {"kind": "quirk", "fields": {"description": "x y z"},
                                  "files": [inside]}),
             ("memory_remember", {"kind": "quirk", "fields": {"description": "p q r"},
                                  "files": ["/etc/passwd"]}),
             ("memory_remember", {"kind": "quirk", "fields": {"description": "s t u"},
                                  "files": [r"..\..\outside.py"]}),
             ("memory_session_start", {"task": "t"}),
             ("memory_recall", {"task": "anything", "files": ["src/cache.py"]})]
    blob = "\n".join(rpc(client, rw, "tools/call", {"name": name, "arguments": args}).text
                     for name, args in calls)
    # a JSON body escapes backslashes: look for the raw, the slash and the escaped form
    variants = {str(tmp_path), tmp_path.as_posix(), json.dumps(str(tmp_path))[1:-1]}
    assert not any(variant in blob for variant in variants)
    assert "path_outside_repo" in blob


def test_status_names_the_store_and_repo_without_their_directories(app_client, tokens):
    client, _ = app_client
    ro = tokens.create("bob", "read-only")
    _, status = tool(client, ro, "memory_status")
    assert status["store"] == "project.memory" and status["repo_root"] == "repo"


def test_host_and_origin_checks(app_client, tokens):
    client, _ = app_client
    token = tokens.create("alice", "read-write")
    assert rpc(client, token, "tools/list", headers={"Host": "evil.example.org"}).status_code == 421
    assert rpc(client, token, "tools/list", headers={"Origin": "http://evil.example.org"}).status_code == 403
    assert rpc(client, token, "tools/list", headers={"Host": "mem.example.com"}).status_code == 200
    assert rpc(client, token, "tools/list", headers={"Host": "localhost:9"}).status_code == 200


def test_the_audit_log_has_names_tools_and_outcomes_but_no_content(app_client, tokens, caplog):
    client, _ = app_client
    rw, ro = tokens.create("alice", "read-write"), tokens.create("bob", "read-only")
    with caplog.at_level(logging.INFO, logger="agent_memory.remote.audit"):
        tool(client, rw, "memory_remember", kind="quirk",
             fields={"description": "SECRET-NOTE-TEXT"}, evidence="SECRET-EVIDENCE")
        tool(client, ro, "memory_remember", kind="quirk", fields={"description": "SECRET-NOTE-TEXT"})
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "name=alice tool=memory_remember outcome=ok" in text
    assert "name=bob tool=memory_remember outcome=forbidden" in text
    assert "SECRET" not in text and rw not in text and ro not in text


def test_a_revoked_tokens_service_is_closed_at_the_next_call(app_client, tokens):
    client, principals = app_client
    a, b = tokens.create("alice", "read-write"), tokens.create("bob", "read-write")
    tool(client, a, "memory_status")
    tool(client, b, "memory_status")
    assert principals.open_count == 2
    tokens.revoke("alice")
    tool(client, b, "memory_status")
    assert principals.open_count == 1


def test_one_service_per_principal_even_when_first_requests_race(base, tokens):
    principals = Principals(base, tokens)
    tokens.create("alice", "read-write")
    entry = tokens.entries()[0]
    principal = Principal("alice", "read-write", entry.created_at)
    barrier = threading.Barrier(6)

    def grab(_):
        barrier.wait()
        return principals.service_for(principal)

    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        services = list(pool.map(grab, range(6)))
    principals.close()
    assert len({id(s) for s in services}) == 1 and principals.open_count == 0


def test_a_read_only_view_has_three_methods_and_no_write_path(base, tokens):
    principals = Principals(base, tokens)
    tokens.create("bob", "read-only")
    view = principals.service_for(Principal("bob", "read-only", tokens.entries()[0].created_at))
    principals.close()
    assert isinstance(view, ReadOnlyView)
    public = {name for name in dir(view) if not name.startswith("_")}
    assert public == {"recall", "check", "status"}


def test_a_closed_principals_refuses_new_services(base, tokens):
    principals = Principals(base, tokens)
    principals.close()
    from agent_memory.service import ServiceError
    with pytest.raises(ServiceError):
        principals.service_for(Principal("x", "read-write", "t"))


@pytest.mark.parametrize("host,expected", [("127.0.0.1", True), ("localhost", True), ("::1", True),
                                           ("[::1]", True), ("127.0.0.5", True), ("0.0.0.0", False),
                                           ("192.168.1.5", False), ("mem.example.com", False), ("", False)])
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


def test_startup_refuses_a_public_bind_without_the_tls_acknowledgement(base, tokens):
    tokens.create("alice", "read-write")
    with pytest.raises(StartupError, match="TLS"):
        check_startup(base, tokens, "0.0.0.0", behind_tls=False)
    check_startup(base, tokens, "0.0.0.0", behind_tls=True)
    check_startup(base, tokens, "127.0.0.1", behind_tls=False)


def test_startup_refuses_when_there_is_no_usable_token(base, tokens):
    with pytest.raises(StartupError, match="amem token create"):
        check_startup(base, tokens, "127.0.0.1", behind_tls=False)
    tokens.create("old", "read-write", expires_in_days=1,
                  now=datetime(2020, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(StartupError):
        check_startup(base, tokens, "127.0.0.1", behind_tls=False)


def test_startup_refuses_a_damaged_token_file_and_a_missing_repo(base, tokens, tmp_path):
    tokens.create("alice", "read-write")
    tokens.path.write_text("{", encoding="utf-8")
    with pytest.raises(StartupError, match="token file"):
        check_startup(base, tokens, "127.0.0.1", behind_tls=False)
    tokens.path.unlink()
    tokens.create("alice", "read-write")
    from dataclasses import replace
    with pytest.raises(StartupError, match="repo"):
        check_startup(replace(base, repo_root=str(tmp_path / "nowhere")), tokens, "127.0.0.1", False)


def test_the_failure_limiter_is_wired_in(base, tokens):
    app, principals = build_app(base, tokens, limiter=FailureLimiter(2, 60.0))
    good = tokens.create("alice", "read-write")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        for _ in range(2):
            assert rpc(client, "amt_bad", "tools/list").status_code == 401
        assert rpc(client, "amt_bad", "tools/list").status_code == 429
        assert rpc(client, good, "tools/list").status_code == 200
    principals.close()


def test_a_token_file_outage_keeps_every_service_and_a_revoked_one_goes_when_it_reads_again(base, tokens):
    import os
    principals = Principals(base, tokens)
    tokens.create("alice", "read-write")
    tokens.create("bob", "read-write")
    entries = {e.name: Principal(e.name, e.role, e.created_at) for e in tokens.entries()}
    principals.service_for(entries["alice"])
    principals.service_for(entries["bob"])
    assert principals.open_count == 2
    good = tokens.path.read_text(encoding="utf-8")
    tokens.path.write_text("{", encoding="utf-8")                  # the outage
    assert tokens.error
    for _ in range(3):
        principals.service_for(entries["bob"])
        assert principals.open_count == 2                          # nothing is torn down meanwhile
    stat = os.stat(tokens.path)
    tokens.path.write_text(good, encoding="utf-8")                 # the file reads cleanly again
    os.utime(tokens.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert not tokens.error
    principals.service_for(entries["bob"])
    assert principals.open_count == 2                              # both tokens are still live
    tokens.revoke("alice")
    principals.service_for(entries["bob"])
    assert principals.open_count == 1                              # only the revoked one went
    principals.close()
