import asyncio
import json
import os
import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters, types          # noqa: E402
from mcp.client.stdio import stdio_client                           # noqa: E402

from agent_memory import AgentMemory                                 # noqa: E402
from storage_helpers import SRC                                      # noqa: E402


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


def test_two_mcp_servers_on_one_store_share_memory_with_attribution(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
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
                    first = await call(a, "memory_remember", kind="convention",
                                       fields={"category": "naming",
                                               "rule": "cache keys use colon namespaces"})
                    seen_by_b = await call(b, "memory_recall", task="how do I name cache keys")
                    same = await call(b, "memory_remember", kind="convention",
                                      fields={"category": "naming",
                                              "rule": "cache keys use colon namespaces"})
                    mine = await call(b, "memory_remember", kind="decision",
                                      fields={"title": "Use Postgres for billing",
                                              "context": "transactions"})
                    seen_by_a = await call(a, "memory_recall", task="which database for billing")
                    return first, seen_by_b, same, mine, seen_by_a

    try:
        first, seen_by_b, same, mine, seen_by_a = asyncio.run(asyncio.wait_for(scenario(), 120))
    except Exception as exc:
        # the servers' own stderr says why (a failed start, a traceback in a handler)
        tail = log.read_text(encoding="utf-8", errors="replace")[-2000:] if log.exists() else ""
        raise AssertionError(
            f"{type(exc).__name__}: {exc}\nserver stderr (tail):\n{tail}") from exc
    assert seen_by_b["items"] and seen_by_b["items"][0]["agent"] == "claude-code"
    assert same["merged"] is True and same["id"] == first["id"]          # one record, not two
    assert any(i["id"] == mine["id"] and i["agent"] == "cursor" for i in seen_by_a["items"])
    final = AgentMemory.load(str(store))
    assert len(list(final.graph.nodes("convention"))) == 1
