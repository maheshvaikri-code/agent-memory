import asyncio
import json

import pytest

pytest.importorskip("mcp")

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from agent_memory.mcp_server import TOOL_NAMES, build_server            # noqa: E402
from agent_memory.protocol import block                                  # noqa: E402
from agent_memory.service import MemoryService, resolve_config           # noqa: E402

TEXT = "the zorblax queue drops items over 64KB"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 90))


def lesson(text=TEXT, **change):
    base = {"kind": "quirk", "fields": {"description": text}, "evidence": "seen in a test run",
            "files": ["src/cache.py"]}
    base.update(change)
    return base


def with_client(repo, scenario):
    service = MemoryService(resolve_config(repo=str(repo), env={}))

    async def go():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            return await scenario(client)

    try:
        return run(go()), service
    finally:
        service.close()


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    return result.isError, result.content[0].text


def test_the_surface_is_the_eleven_tools_and_none_of_them_can_accept(repo):
    async def scenario(client):
        return (await client.list_tools()).tools

    tools, _ = with_client(repo, scenario)
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES) and len(TOOL_NAMES) == 11
    assert "memory_capture_lessons" in TOOL_NAMES
    for name in TOOL_NAMES:
        assert not any(word in name for word in ("review", "accept", "reject", "promote"))
    assert all(len(t.description) <= 400 for t in tools)
    assert sum(len(t.description) for t in tools) <= 3000


def test_lessons_are_a_required_list_on_the_capture_tool_and_optional_on_session_end(repo):
    async def scenario(client):
        return {t.name: t for t in (await client.list_tools()).tools}

    tools, _ = with_client(repo, scenario)
    assert tools["memory_capture_lessons"].inputSchema["required"] == ["lessons"]
    end = tools["memory_session_end"].inputSchema
    assert "lessons" in end["properties"] and "lessons" not in end.get("required", [])
    assert "lessons" in tools["memory_session_end"].description
    assert "person" in tools["memory_capture_lessons"].description


def test_a_captured_lesson_is_not_recalled(repo):
    async def scenario(client):
        err, text = await call(client, "memory_capture_lessons", lessons=[lesson()])
        assert not err, text
        captured = json.loads(text)
        err, recalled = await call(client, "memory_recall", task="zorblax queue drops items")
        assert not err, recalled
        return captured, json.loads(recalled)

    (captured, recalled), _ = with_client(repo, scenario)
    assert len(captured["captured"]) == 1 and captured["pending"] == 1
    assert recalled["items"] == []


def test_session_end_takes_lessons_and_a_json_looking_evidence_survives(repo):
    evidence = '{"error": "StripeSignatureError: no signatures found"}'

    async def scenario(client):
        await call(client, "memory_session_start", task="t")
        err, text = await call(client, "memory_session_end", summary="done",
                               lessons=[lesson(evidence=evidence)])
        assert not err, text
        return json.loads(text)

    out, service = with_client(repo, scenario)
    assert out["ok"] is True and len(out["capture"]["captured"]) == 1


def test_a_full_queue_is_a_tool_error_with_the_code(repo, monkeypatch):
    from agent_memory.service import service as service_module
    monkeypatch.setattr(service_module, "MAX_PENDING", 1)

    async def scenario(client):
        await call(client, "memory_capture_lessons", lessons=[lesson("filler lesson")])
        return await call(client, "memory_capture_lessons", lessons=[lesson()])

    (is_error, text), _ = with_client(repo, scenario)
    assert is_error and "queue_full" in text


def test_the_mcp_block_tells_agents_about_lessons_and_the_person():
    text = block("mcp")
    assert "lessons" in text and "person" in text
