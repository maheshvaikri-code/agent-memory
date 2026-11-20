import asyncio
import json
import os
import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters, types          # noqa: E402
from mcp.client.stdio import stdio_client                           # noqa: E402

from agent_memory.service import MemoryService, resolve_config      # noqa: E402
from storage_helpers import SRC                                      # noqa: E402

TEXT = "the zorblax queue drops items over 64KB"


def params(store, repo):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([SRC, env.get("PYTHONPATH", "")])
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_memory.cli", "serve", "--store", str(store), "--repo", str(repo)],
        env=env, cwd=str(repo))


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


def test_a_lesson_captured_on_one_server_stays_invisible_to_the_other_until_accepted(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "cache.py").write_text("x = 1\n")
    store = tmp_path / "shared.memory"
    log = tmp_path / "servers.stderr"

    async def scenario():
        with open(log, "w", encoding="utf-8") as errlog:
            async with stdio_client(params(store, repo), errlog=errlog) as (r1, w1), \
                       stdio_client(params(store, repo), errlog=errlog) as (r2, w2):
                async with ClientSession(r1, w1, client_info=types.Implementation(
                        name="claude-code", version="1")) as a, \
                        ClientSession(r2, w2, client_info=types.Implementation(
                            name="cursor", version="1")) as b:
                    await a.initialize()
                    await b.initialize()
                    captured = await call(a, "memory_capture_lessons", lessons=[{
                        "kind": "quirk", "fields": {"description": TEXT},
                        "evidence": "seen in a test run", "files": ["src/cache.py"]}])
                    ident = captured["captured"][0]
                    before = await call(b, "memory_recall", task="zorblax queue drops items")
                    checked = await call(b, "memory_check",
                                         approach="zorblax queue drops items over 64KB")
                    person = MemoryService(resolve_config(store=str(store), repo=str(repo),
                                                          agent="person"))
                    try:
                        person.review_accept(ident)
                    finally:
                        person.close()
                    after = await call(b, "memory_check",
                                       approach="zorblax queue drops items over 64KB")
                    return ident, before, checked, after

    try:
        ident, before, checked, after = asyncio.run(asyncio.wait_for(scenario(), 120))
    except Exception as exc:
        tail = log.read_text(encoding="utf-8", errors="replace")[-2000:] if log.exists() else ""
        raise AssertionError(f"{type(exc).__name__}: {exc}\nserver stderr (tail):\n{tail}") from exc
    assert before["items"] == [] and checked["items"] == []
    assert [i["id"] for i in after["items"]] == [ident]
    assert after["items"][0]["unreviewed"] is False
