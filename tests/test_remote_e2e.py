import asyncio
import json
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("mcp")
import uvicorn                                                        # noqa: E402
from mcp import ClientSession, types                                  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client      # noqa: E402
from mcp.client.streamable_http import streamablehttp_client          # noqa: E402

from agent_memory.remote.server import build_app                      # noqa: E402
from agent_memory.remote.tokens import TokenFile                      # noqa: E402
from agent_memory.service import MemoryService, resolve_config                       # noqa: E402
from storage_helpers import child_env                            # noqa: E402


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live(repo, tmp_path):
    base = resolve_config(repo=str(repo), agent="remote", env={})
    tokens = TokenFile(tmp_path / "tokens.json")
    app, principals = build_app(base, tokens)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}/mcp", tokens, base
    server.should_exit = True
    thread.join(10)
    principals.close()


@pytest.fixture
def live_server(repo, tmp_path):
    """Like `live`, but also hands the test the uvicorn server and its thread, so the test can
    stop the server itself while a call is running."""
    base = resolve_config(repo=str(repo), agent="remote", env={})
    tokens = TokenFile(tmp_path / "tokens.json")
    app, principals = build_app(base, tokens)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}/mcp", tokens, base, server, thread
    finally:
        server.should_exit = True
        thread.join(30)
        principals.close()


def wait_listening(port, proc, seconds=30):
    """Block until the child accepts a connection; fail loudly if it never does or it died."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        assert proc.poll() is None, f"the server exited early (rc={proc.returncode})"
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"the server did not listen on port {port} within {seconds} s")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 90))


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    return result.isError, result.content[0].text


def test_a_real_client_writes_with_one_token_and_reads_with_another(live):
    url, tokens, _ = live
    rw, ro = tokens.create("alice", "read-write"), tokens.create("bob", "read-only")

    async def scenario():
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {rw}"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()
                names = [t.name for t in (await client.list_tools()).tools]
                made = await call(client, "memory_remember", kind="quirk",
                                  fields={"description": "the zorblax queue drops items over 64KB"},
                                  files=["src/cache.py"])
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {ro}"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()
                found = await call(client, "memory_recall", task="zorblax queue drops items")
                refused = await call(client, "memory_retract", id=json.loads(made[1])["id"])
        return names, made, found, refused

    names, made, found, refused = run(scenario())
    assert len(names) == 11
    assert not made[0] and json.loads(made[1])["created"] is True
    assert not found[0] and json.loads(found[1])["items"][0]["id"] == json.loads(made[1])["id"]
    assert refused[0] and "forbidden" in refused[1]


def test_a_wrong_token_never_gets_a_session(live):
    url, tokens, _ = live
    tokens.create("alice", "read-write")

    async def scenario():
        async with streamablehttp_client(url, headers={"Authorization": "Bearer amt_nope"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()

    with pytest.raises(BaseException) as refused:
        run(scenario())
    assert "401 Unauthorized" in repr(refused.value)    # refused by the guard, not some other failure


def test_revoking_a_token_cuts_a_running_server_off_without_a_restart(live):
    url, tokens, _ = live
    token = tokens.create("alice", "read-write")

    async def attempt():
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()
                return await call(client, "memory_status")

    assert not run(attempt())[0]
    tokens.revoke("alice")
    with pytest.raises(BaseException) as refused:
        run(attempt())
    assert "401 Unauthorized" in repr(refused.value)


def test_a_stdio_server_and_the_http_server_share_one_store(live, repo):
    url, tokens, base = live
    token = tokens.create("alice", "read-write")
    params = StdioServerParameters(command=sys.executable, args=["-m", "agent_memory.cli", "serve",
                                   "--repo", str(repo)], env=child_env())

    stdio_phrase, http_phrase = "the quuxblat cache evicts early", "the zibblefrit retry loop stalls"

    async def scenario():
        async with stdio_client(params) as (sr, sw):
            async with ClientSession(sr, sw, client_info=types.Implementation(name="claude-code", version="1")) as local:
                await local.initialize()
                wrote_stdio = await call(local, "memory_remember", kind="quirk",
                                         fields={"description": stdio_phrase})
                async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
                    async with ClientSession(r, w) as remote:
                        await remote.initialize()
                        http_sees_stdio = await call(remote, "memory_recall", task="quuxblat cache evicts early")
                        http_sees_other = await call(remote, "memory_recall", task="zibblefrit retry loop stalls")
                        wrote_http = await call(remote, "memory_remember", kind="quirk",
                                                fields={"description": http_phrase})
                stdio_sees_http = await call(local, "memory_recall", task="zibblefrit retry loop stalls")
                stdio_sees_own = await call(local, "memory_recall", task="quuxblat cache evicts early")
                return (wrote_stdio, wrote_http, http_sees_stdio, http_sees_other, stdio_sees_http,
                        stdio_sees_own)

    wrote_stdio, wrote_http, http_sees_stdio, http_sees_other, stdio_sees_http, stdio_sees_own = run(scenario())
    stdio_id, http_id = json.loads(wrote_stdio[1])["id"], json.loads(wrote_http[1])["id"]
    assert stdio_id != http_id

    def ids(result):
        return [item["id"] for item in json.loads(result[1])["items"]]

    assert ids(http_sees_stdio) == [stdio_id]        # stdio to http: the stdio note, nothing else
    assert ids(http_sees_other) == []                # the http note did not exist yet: no false match
    assert ids(stdio_sees_http) == [http_id]         # http to stdio: only the note the http side wrote
    assert ids(stdio_sees_own) == [stdio_id]         # and the other note is not returned for its words
    # who wrote each: the stdio client's own name, and the token's name for the http writer
    service = MemoryService(resolve_config(repo=str(repo), agent="check", env={}))
    try:
        authors = {item["id"]: item["agent"] for item in
                   (service.recall(task="quuxblat cache evicts early")["items"]
                    + service.recall(task="zibblefrit retry loop stalls")["items"])}
    finally:
        service.close()
    assert authors == {stdio_id: "claude-code", http_id: "alice"}


def test_amem_serve_http_serves_and_audits(repo, tmp_path):
    from agent_memory.remote.tokens import TokenFile as TF
    tokens = TF(tmp_path / "tokens.json")
    token = tokens.create("alice", "read-write")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_memory.cli", "serve", "--http", "--port", str(port),
         "--repo", str(repo), "--tokens", str(tokens.path)],
        env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        wait_listening(port, proc)

        async def scenario():
            async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
                async with ClientSession(r, w) as client:
                    await client.initialize()
                    return await call(client, "memory_status")

        is_error, text = run(scenario())
        assert not is_error and json.loads(text)["agent"] == "alice"
        with pytest.raises(urllib.error.HTTPError) as denied:      # the 401 names no server software
            urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST"), timeout=10)
        assert denied.value.code == 401 and denied.value.headers.get("server") is None
    finally:
        proc.terminate()
        try:
            _, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
    assert token not in err and "Traceback" not in err
    assert "name=alice tool=memory_status outcome=ok" in err
    assert err.count("agent-memory remote: listening on http://127.0.0.1:") == 1
    assert re.search(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ call name=alice ", err, re.M)
    # No snapshot assertion: the store file exists from start-up, so it would pass vacuously, and
    # terminate() is a hard kill on Windows. A graceful stop leaving a checkpoint is not covered here.


def test_a_graceful_stop_folds_the_journal_and_keeps_the_note(repo, tmp_path):
    """SIGTERM (or Ctrl-Break on Windows) ends the process through uvicorn's signal handling,
    which re-raises the signal after shutting down; the services must be closed before that."""
    tokens = TokenFile(tmp_path / "tokens.json")
    token = tokens.create("alice", "read-write")
    port = free_port()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_memory.cli", "serve", "--http", "--port", str(port),
         "--repo", str(repo), "--tokens", str(tokens.path)],
        env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=flags)
    err = ""
    try:
        wait_listening(port, proc)

        async def scenario():
            async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp",
                                             headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
                async with ClientSession(r, w) as client:
                    await client.initialize()
                    made = await call(client, "memory_remember", kind="quirk",
                                      fields={"description": "the zorblax queue drops items over 64KB"})
                    status = await call(client, "memory_status")
                    return made, json.loads(status[1])["unfolded_transactions"]

        made, unfolded = run(scenario())
        assert not made[0] and unfolded > 0                  # in the journal, not yet folded
    finally:
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        try:
            _, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
    assert token not in err and "Traceback" not in err
    service = MemoryService(resolve_config(repo=str(repo), agent="check", env={}))
    try:
        assert service.status()["unfolded_transactions"] == 0, f"no checkpoint on stop (rc={proc.returncode})"
        found = service.recall(task="zorblax queue drops items")
        assert any("zorblax" in json.dumps(item) for item in found["items"])
    finally:
        service.close()


def test_a_held_call_does_not_stop_other_callers_on_a_real_server(live, monkeypatch):
    url, tokens, _ = live
    a, b = tokens.create("alice", "read-write"), tokens.create("bob", "read-write")
    entered, release = threading.Event(), threading.Event()
    original = MemoryService._context

    def held(self, context):
        entered.set()
        release.wait(20)
        return original(self, context)

    monkeypatch.setattr(MemoryService, "_context", held)
    result = {}

    def hold_one():
        async def go():
            async with streamablehttp_client(url, headers={"Authorization": f"Bearer {a}"}) as (r, w, _):
                async with ClientSession(r, w) as client:
                    await client.initialize()
                    return await call(client, "memory_recall", task="anything")
        result["alice"] = run(go())

    async def bob_status():
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {b}"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()
                return await call(client, "memory_status")

    holder = threading.Thread(target=hold_one)
    holder.start()
    try:
        assert entered.wait(20)
        started = time.monotonic()
        request = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=10)
        # The bounds only have to tell "answered while the call is held" from "waited for it" (the
        # hold lasts up to 20 s), so they are generous: a full-suite run on a loaded machine is slow.
        assert refused.value.code == 401 and time.monotonic() - started < 5.0
        started = time.monotonic()
        failed, _text = run(bob_status())
        assert not failed and time.monotonic() - started < 10.0
    finally:
        release.set()
        holder.join(30)
    assert not result["alice"][0]


def test_a_stop_with_a_call_in_flight_delivers_its_result_and_leaves_a_checkpoint(
        live_server, repo, monkeypatch):
    """A normal stop waits for the running call, hands its result to the client, and only then
    closes the services (no lease is held by then), each leaving a checkpoint."""
    url, tokens, _base, server, thread = live_server
    token = tokens.create("alice", "read-write")

    async def write_note():
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()
                made = await call(client, "memory_remember", kind="quirk",
                                  fields={"description": "the zorblax queue drops items over 64KB"})
                status = await call(client, "memory_status")
                return made, json.loads(status[1])["unfolded_transactions"]

    made, unfolded = run(write_note())
    assert not made[0] and unfolded > 0                      # in the journal, not yet folded

    entered, release = threading.Event(), threading.Event()
    original = MemoryService._context

    def held(self, context):
        entered.set()
        release.wait(20)
        return original(self, context)

    monkeypatch.setattr(MemoryService, "_context", held)
    result = {}

    def hold_one():
        async def go():
            async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
                async with ClientSession(r, w) as client:
                    await client.initialize()
                    await client.list_tools()                # cache the output schemas first: the client
                    #                                          would otherwise send a follow-up request
                    #                                          after the call, which a stopping server
                    #                                          no longer answers
                    return await call(client, "memory_recall", task="zorblax queue")
        try:
            result["call"] = run(go())
        except BaseException as exc:                         # noqa: BLE001
            result["error"] = repr(exc)

    holder = threading.Thread(target=hold_one)
    holder.start()
    try:
        assert entered.wait(20)
        server.should_exit = True                            # stop while the call is inside the service
        time.sleep(1.5)
        assert thread.is_alive()                             # the stop is waiting for the call
    finally:
        release.set()
    released = time.monotonic()
    thread.join(15)
    holder.join(30)
    assert not thread.is_alive() and time.monotonic() - released < 15
    assert "error" not in result, result.get("error")
    assert not result["call"][0], result["call"][1]          # the call's result reached the client
    service = MemoryService(resolve_config(repo=str(repo), agent="check", env={}))
    try:
        assert service.status()["unfolded_transactions"] == 0   # every service left a checkpoint
    finally:
        service.close()
