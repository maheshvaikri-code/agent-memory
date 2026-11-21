import asyncio
import json

import pytest

mcp = pytest.importorskip("mcp")

from mcp import types                                                # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from agent_memory.mcp_server import (READ_ONLY_TOOLS, TOOL_METHODS, TOOL_NAMES,   # noqa: E402
                                     build_server)
from agent_memory.service import MemoryService, ServiceError, resolve_config  # noqa: E402

WRITE_CALLS = {
    "memory_remember": {"kind": "quirk", "fields": {"description": "d"}},
    "memory_feedback": {"id": "x", "helpful": True},
    "memory_supersede": {"old_id": "x", "kind": "quirk", "fields": {"description": "d"}},
    "memory_retract": {"id": "x"},
    "memory_verify": {"id": "x"},
    "memory_session_start": {"task": "t"},
    "memory_session_end": {},
    "memory_capture_lessons": {"lessons": [{"kind": "quirk", "fields": {"description": "d"},
                                            "evidence": "e", "files": ["src/cache.py"]}]},
}


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


def test_every_tool_is_classified_and_backed_by_a_service_method():
    assert tuple(TOOL_METHODS) == TOOL_NAMES and len(TOOL_NAMES) == 11
    assert READ_ONLY_TOOLS == {"memory_recall", "memory_check", "memory_status"}
    assert set(WRITE_CALLS) == set(TOOL_NAMES) - READ_ONLY_TOOLS
    for method in TOOL_METHODS.values():
        assert callable(getattr(MemoryService, method))


def test_a_read_only_caller_is_refused_every_write_tool_and_nothing_is_written(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="ro", env={}))

    async def scenario():
        server = build_server(provider=lambda: (service, "read-only"))
        async with create_connected_server_and_client_session(server) as client:
            refused = {name: await call(client, name, **args) for name, args in WRITE_CALLS.items()}
            allowed = [await call(client, "memory_recall", task="anything"),
                       await call(client, "memory_check", approach="anything"),
                       await call(client, "memory_status")]
            return refused, allowed

    refused, allowed = run(scenario())
    try:
        assert service.status()["total_active"] == 0 and service.status()["pending"] == 0
    finally:
        service.close()
    assert all(is_error and "forbidden" in text for is_error, text in refused.values())
    assert all(not is_error for is_error, _ in allowed)


def test_a_read_write_provider_can_use_every_tool(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="rw", env={}))

    async def scenario():
        server = build_server(provider=lambda: (service, "read-write"))
        async with create_connected_server_and_client_session(server) as client:
            return await call(client, "memory_remember", kind="quirk",
                              fields={"description": "a thing"})

    is_error, text = run(scenario())
    service.close()
    assert not is_error and json.loads(text)["created"] is True


def test_the_client_reported_name_is_ignored_when_not_trusted(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="alice", env={}))

    async def scenario():
        server = build_server(provider=lambda: (service, "read-write"), trust_client_name=False)
        async with create_connected_server_and_client_session(
                server, client_info=types.Implementation(name="evil-bot", version="1")) as client:
            return await call(client, "memory_remember", kind="quirk",
                              fields={"description": "who wrote me"})

    _, text = run(scenario())
    node = service.memory.graph.get_node("quirk", json.loads(text)["id"])
    service.close()
    assert node.properties["agent"] == "alice"


def test_the_client_reported_name_is_still_used_for_stdio(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="cli", env={}))

    async def scenario():
        async with create_connected_server_and_client_session(
                build_server(service),
                client_info=types.Implementation(name="claude-code", version="1")) as client:
            return await call(client, "memory_remember", kind="quirk",
                              fields={"description": "who wrote me"})

    _, text = run(scenario())
    node = service.memory.graph.get_node("quirk", json.loads(text)["id"])
    service.close()
    assert node.properties["agent"] == "claude-code"


def test_the_observer_sees_every_call_with_its_outcome(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="ro", env={}))
    seen = []

    async def scenario():
        server = build_server(provider=lambda: (service, "read-only"),
                              observer=lambda tool, outcome, ms: seen.append((tool, outcome, ms)))
        async with create_connected_server_and_client_session(server) as client:
            await call(client, "memory_recall", task="t")
            await call(client, "memory_retract", id="x")

    run(scenario())
    service.close()
    assert [(t, o) for t, o, _ in seen] == [("memory_recall", "ok"), ("memory_retract", "forbidden")]
    assert all(isinstance(ms, int) and ms >= 0 for _, _, ms in seen)


def test_error_messages_pass_through_the_scrubber(repo):
    class Broken:
        def set_client(self, name):
            pass

        def status(self):
            raise ServiceError("store_corrupt", "cannot read C:/secret/dir/project.memory")

    async def scenario():
        server = build_server(provider=lambda: (Broken(), "read-write"),
                              scrub=lambda text: text.replace("C:/secret/dir", "<dir>"))
        async with create_connected_server_and_client_session(server) as client:
            return await call(client, "memory_status")

    is_error, text = run(scenario())
    assert is_error and "C:/secret" not in text and "<dir>/project.memory" in text


def test_a_provider_that_fails_closed_gives_an_error_not_a_crash(repo):
    def provider():
        raise ServiceError("forbidden", "no authenticated caller")

    async def scenario():
        async with create_connected_server_and_client_session(build_server(provider=provider)) as c:
            return await call(c, "memory_status")

    is_error, text = run(scenario())
    assert is_error and "forbidden" in text


def test_build_server_needs_exactly_one_of_service_and_provider(repo):
    with pytest.raises(ValueError):
        build_server()
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    try:
        with pytest.raises(ValueError):
            build_server(service, provider=lambda: (service, "read-write"))
    finally:
        service.close()
