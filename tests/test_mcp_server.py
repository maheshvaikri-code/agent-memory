import asyncio
import json
import os
import subprocess
import sys

import pytest

mcp = pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters, types          # noqa: E402
from mcp.client.stdio import stdio_client                           # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from agent_memory import AgentMemory                                 # noqa: E402
from agent_memory.mcp_server import TOOL_NAMES, build_server        # noqa: E402
from agent_memory.protocol import block                              # noqa: E402
from agent_memory.service import MemoryService, resolve_config       # noqa: E402
from storage_helpers import SRC                                      # noqa: E402


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 90))


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    return result.isError, result.content[0].text


def test_the_tool_surface_is_exactly_the_specified_set(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}))

    async def scenario():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            return (await client.list_tools()).tools

    tools = run(scenario())
    service.close()
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES) and len(TOOL_NAMES) == 10
    assert all(t.description and len(t.description) <= 400 for t in tools)
    assert sum(len(t.description) for t in tools) <= 3000       # tokens are paid on every turn


def test_recall_takes_an_optional_free_text_context(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}))

    async def scenario():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in run(scenario())}
    service.close()
    schema = tools["memory_recall"].inputSchema
    assert "context" in schema["properties"]
    assert "context" not in schema.get("required", [])
    assert schema["required"] == ["task"]
    assert "context" in tools["memory_recall"].description


def test_a_full_round_trip_through_the_protocol(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}))

    async def scenario():
        async with create_connected_server_and_client_session(
                build_server(service),
                client_info=types.Implementation(name="claude-code", version="1")) as client:
            out = {}
            _, text = await call(client, "memory_session_start", task="add retries")
            out["start"] = json.loads(text)
            _, text = await call(client, "memory_remember", kind="bug_pattern",
                                 fields={"name": "retry_storm",
                                         "signature": "unbounded retry on an external webhook"},
                                 files=["src/cache.py"])
            out["remember"] = json.loads(text)
            _, text = await call(client, "memory_recall", task="retry on the webhook",
                                 files=["src/cache.py"])
            out["recall"] = json.loads(text)
            _, text = await call(client, "memory_check", approach="retry unbounded on the webhook")
            out["check"] = json.loads(text)
            _, text = await call(client, "memory_feedback", id=out["remember"]["id"], helpful=True)
            out["feedback"] = json.loads(text)
            _, text = await call(client, "memory_status")
            out["status"] = json.loads(text)
            _, text = await call(client, "memory_session_end", summary="added retry notes")
            out["end"] = json.loads(text)
            return out

    out = run(scenario())
    service.close()
    assert out["start"]["session_id"] and out["remember"]["created"] is True
    assert out["recall"]["items"] and out["recall"]["prompt"].startswith("<recorded-project-notes")
    assert out["check"]["warn"] is True and out["feedback"]["ok"] is True
    assert out["status"]["agent"] == "claude-code" and out["status"]["session"]
    assert out["end"]["ok"] is True
    node = AgentMemory.load(service.config.store_path).graph.get_node(
        "bug_pattern", out["remember"]["id"])
    assert node.properties["agent"] == "claude-code"           # the client's own name


def test_recall_context_passes_through_the_protocol(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    trace = ("Traceback (most recent call last):\n"
             '  File "/srv/app/payments/hooks.py", line 31, in receive\n'
             "    event = construct_event(payload, header)\n"
             "StripeSignatureError: No signatures found matching the expected signature\n")

    async def scenario():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            _, text = await call(client, "memory_remember", kind="convention", fields={
                "category": "payments",
                "rule": "StripeSignatureError means the endpoint secret rotated; reload it"})
            remembered = json.loads(text)
            _, text = await call(client, "memory_recall", task="the checkout suite is red again")
            alone = json.loads(text)
            is_error, text = await call(client, "memory_recall",
                                        task="the checkout suite is red again", context=trace)
            with_context = (is_error, json.loads(text))
            bad = await call(client, "memory_recall", task="t", context="x" * 2001)
            return remembered, alone, with_context, bad

    remembered, alone, (is_error, lifted), (bad_error, bad_text) = run(scenario())
    service.close()
    assert alone["items"] == []                                  # the task alone abstains
    assert is_error is False and [i["id"] for i in lifted["items"]] == [remembered["id"]]
    assert bad_error is True and '"code": "too_large"' in bad_text.replace('\\"', '"')


def test_service_errors_arrive_as_tool_errors_with_a_stable_code(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}))

    async def scenario():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            bad_kind = await call(client, "memory_remember", kind="module", fields={"path": "x"})
            outside = await call(client, "memory_recall", task="t", files=["../x.py"])
            missing = await call(client, "memory_feedback", id="nope", helpful=True)
            return bad_kind, outside, missing

    bad_kind, outside, missing = run(scenario())
    service.close()
    for (is_error, text), code in ((bad_kind, "unknown_kind"), (outside, "path_outside_repo"),
                                   (missing, "not_found")):
        assert is_error is True and f'"code": "{code}"' in text.replace('\\"', '"')


def test_an_unexpected_failure_is_a_generic_internal_error(repo, monkeypatch):
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    secret = os.path.join(str(repo), "secret", "path.py")

    def boom():
        raise RuntimeError(f"failed reading {secret}")

    monkeypatch.setattr(service, "status", boom)

    async def scenario():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            return await call(client, "memory_status")

    is_error, text = run(scenario())
    service.close()
    assert is_error is True and '"code": "internal"' in text.replace('\\"', '"')
    assert "secret" not in text and "Traceback" not in text and "RuntimeError" not in text


def test_server_instructions_carry_the_protocol(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    server = build_server(service)
    assert server.instructions == block("mcp")
    service.close()


def stdio_params(store, repo):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([SRC, env.get("PYTHONPATH", "")])
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_memory.cli", "serve", "--store", str(store), "--repo", str(repo)],
        env=env, cwd=str(repo))


def test_a_real_stdio_server_round_trip_and_clean_shutdown(repo, tmp_path):
    store = tmp_path / "stdio.memory"

    async def scenario():
        with open(os.devnull, "w") as errlog:
            async with stdio_client(stdio_params(store, repo), errlog=errlog) as (read, write):
                async with ClientSession(read, write, client_info=types.Implementation(
                        name="cursor", version="1")) as client:
                    await client.initialize()
                    names = sorted(t.name for t in (await client.list_tools()).tools)
                    _, text = await call(client, "memory_remember", kind="convention",
                                         fields={"category": "naming",
                                                 "rule": "cache keys use colon namespaces"})
                    remembered = json.loads(text)
                    _, text = await call(client, "memory_recall", task="name cache keys")
                    return names, remembered, json.loads(text)

    names, remembered, recalled = run(scenario())
    assert names == sorted(TOOL_NAMES)
    assert remembered["created"] is True and recalled["items"][0]["agent"] == "cursor"
    # a stopped server leaves a complete snapshot: the journal holds no unfolded transactions
    from agent_memory.storage import journal
    deadline = 100
    while deadline and journal.read_tail(str(store) + ".journal", 0).txs:
        import time
        time.sleep(0.1)
        deadline -= 1
    assert journal.read_tail(str(store) + ".journal", 0).txs == []
    assert any("colon namespaces" in i.text
               for i in AgentMemory.load(str(store)).recall(task="cache keys").items)


def test_stdout_carries_only_protocol_frames(repo, tmp_path):
    """A stray print in the server would corrupt the stream a client parses."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([SRC, env.get("PYTHONPATH", "")])
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"}}})
    proc = subprocess.run(
        [sys.executable, "-m", "agent_memory.cli", "serve", "--store", str(tmp_path / "s.memory"),
         "--repo", str(repo)],
        input=init + "\n", capture_output=True, text=True, env=env, cwd=str(repo), timeout=60)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, proc.stderr
    for line in lines:
        assert json.loads(line)["jsonrpc"] == "2.0"
