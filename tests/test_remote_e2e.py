import asyncio
import json
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("mcp")
import uvicorn                                                        # noqa: E402
from mcp import ClientSession, types                                  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client      # noqa: E402
from mcp.client.streamable_http import streamablehttp_client          # noqa: E402

from agent_memory.remote.server import build_app                      # noqa: E402
from agent_memory.remote.tokens import TokenFile                      # noqa: E402
from agent_memory.service import resolve_config                       # noqa: E402
from storage_helpers import SRC, child_env                            # noqa: E402


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
    assert "401" in repr(refused.value)                 # refused by the guard, not some other failure


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
    assert "401" in repr(refused.value)


def test_a_stdio_server_and_the_http_server_share_one_store(live, repo):
    url, tokens, base = live
    token = tokens.create("alice", "read-write")
    params = StdioServerParameters(command=sys.executable, args=["-m", "agent_memory.cli", "serve",
                                   "--repo", str(repo)], env=child_env())

    async def scenario():
        async with stdio_client(params) as (sr, sw):
            async with ClientSession(sr, sw, client_info=types.Implementation(name="claude-code", version="1")) as local:
                await local.initialize()
                await call(local, "memory_remember", kind="quirk",
                           fields={"description": "written over stdio, read over http"})
                async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
                    async with ClientSession(r, w) as remote:
                        await remote.initialize()
                        seen = await call(remote, "memory_recall", task="written over stdio read over http")
                        await call(remote, "memory_remember", kind="quirk",
                                   fields={"description": "written over http, read over stdio"})
                back = await call(local, "memory_recall", task="written over http read over stdio")
                return seen, back

    seen, back = run(scenario())
    assert json.loads(seen[1])["items"] and json.loads(back[1])["items"]


def test_amem_serve_http_runs_as_a_process_and_stops_cleanly(repo, tmp_path):
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
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)

        async def scenario():
            async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
                async with ClientSession(r, w) as client:
                    await client.initialize()
                    return await call(client, "memory_status")

        is_error, text = run(scenario())
        assert not is_error and json.loads(text)["agent"] == "alice"
    finally:
        proc.terminate()
        try:
            _, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
    assert token not in err and "Traceback" not in err
    assert "name=alice tool=memory_status outcome=ok" in err
    # No snapshot assertion: the store file exists from start-up, so it would pass vacuously, and
    # terminate() is a hard kill on Windows. A graceful stop leaving a checkpoint is not covered here.


def test_a_graceful_stop_folds_the_journal_and_keeps_the_note(repo, tmp_path):
    """SIGTERM (or Ctrl-Break on Windows) ends the process through uvicorn's signal handling,
    which re-raises the signal after shutting down; the services must be closed before that."""
    from agent_memory.service import MemoryService
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
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)

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
