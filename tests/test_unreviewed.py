"""Guardrail notes nobody has confirmed are marked `unreviewed`.

Rule: an item is unreviewed when its kind is guardrail-tier in the registry and its source is
"explicit" or "extracted" (what agents, and the legacy `amem add` commands, stamp). Every recall
and check item carries the bool; a check with at least one unreviewed item says so in `note`; the
human `amem guard` / `amem recall` output marks those items `[unreviewed]`."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from agent_memory import AgentMemory
from agent_memory.cli import main
from agent_memory.protocol import block
from agent_memory.record import GUARDRAIL, KINDS
from agent_memory.service import MemoryService, resolve_config
from agent_memory.service.shaping import UNREVIEWED_NOTE

BUG = {"name": "retry_storm", "signature": "unbounded retry on an external webhook"}
CONVENTION = {"category": "naming", "rule": "cache keys use colon namespaces"}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def svc(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="tester", env={}))
    yield service
    service.close()


def test_the_note_text():
    assert UNREVIEWED_NOTE == ("These warnings come from notes written by agents and not "
                               "reviewed by a human; confirm them before acting.")


def test_an_agent_guardrail_is_unreviewed_in_check_and_recall(svc):
    svc.remember("bug_pattern", BUG)
    checked = svc.check("retry unbounded on the external webhook")
    assert checked["warn"] is True
    assert [i["unreviewed"] for i in checked["items"]] == [True]
    assert checked["note"] == UNREVIEWED_NOTE
    recalled = svc.recall("retry on the external webhook")
    bugs = [i for i in recalled["items"] if i["kind"] == "bug_pattern"]
    assert bugs and bugs[0]["unreviewed"] is True
    assert recalled["note"] == ""                       # recall's own note is unchanged


def test_non_guardrail_kinds_are_never_unreviewed(svc):
    svc.remember("convention", CONVENTION)
    svc.remember("decision", {"title": "cache keys use colons", "context": "naming review"})
    recalled = svc.recall("how should I name cache keys")
    assert recalled["items"]
    assert all(i["unreviewed"] is False for i in recalled["items"])


@pytest.mark.parametrize("source,expected", [("explicit", True), ("extracted", True),
                                             ("bootstrap", False), ("imported", False),
                                             ("legacy", False)])
def test_the_rule_follows_the_source(repo, source, expected):
    memory = AgentMemory("t", repo_root=str(repo), agent="someone")
    memory.remember("bug_pattern", BUG, source=source)
    service = MemoryService(memory=memory)
    checked = service.check("retry unbounded on the external webhook")
    assert [i["unreviewed"] for i in checked["items"]] == [expected]
    assert checked["note"] == (UNREVIEWED_NOTE if expected else "")


def test_the_tier_comes_from_the_registry(repo):
    # hot_path is guardrail-tier but not an agent kind: the registry decides, not a list
    assert KINDS["hot_path"].tier == GUARDRAIL
    memory = AgentMemory("t", repo_root=str(repo))
    memory.remember("hot_path", {"name": "webhook_dispatch", "frequency": "every request"})
    service = MemoryService(memory=memory)
    items = service.recall("webhook_dispatch hot path every request")["items"]
    assert [i for i in items if i["kind"] == "hot_path"][0]["unreviewed"] is True


def test_a_legacy_cli_bug_pattern_is_marked_too(repo, tmp_path):
    path = tmp_path / "legacy.memory"
    AgentMemory("legacy").save(str(path))
    result = CliRunner().invoke(main, ["add", "bug-pattern", str(path), "-n", BUG["name"],
                                       "-s", BUG["signature"]])
    assert result.exit_code == 0, result.output
    memory = AgentMemory.load(str(path), repo_root=str(repo))
    checked = MemoryService(memory=memory).check("retry unbounded on the external webhook")
    assert checked["items"] and all(i["unreviewed"] for i in checked["items"])


def test_every_item_carries_the_bool_including_carried_ones(svc):
    svc.session_start("first task")
    svc.remember("open_question", {"question": "cap the retries?"})
    ended = svc.session_end()
    assert ended["unresolved"] and all(i["unreviewed"] is False for i in ended["unresolved"])
    svc.remember("bug_pattern", BUG)
    svc.remember("convention", CONVENTION)
    for out in (svc.recall("retry on the external webhook cache keys"),
                svc.check("retry unbounded on the external webhook")):
        assert all(isinstance(i["unreviewed"], bool) for i in out["items"])


def test_the_prompt_itself_is_unchanged(svc):
    svc.remember("bug_pattern", BUG)
    for out in (svc.recall("retry on the external webhook"),
                svc.check("retry unbounded on the external webhook")):
        assert out["prompt"] and "[unreviewed]" not in out["prompt"]


# -- CLI ------------------------------------------------------------------------------

def cli(repo, *args, json_out=True):
    argv = [*args, "--repo", str(repo)] + (["--json"] if json_out else [])
    result = CliRunner().invoke(main, argv)
    assert result.exit_code == 0, result.output
    return json.loads(result.output) if json_out else result.output


def test_human_guard_and_recall_mark_unreviewed_items(repo):
    bug = cli(repo, "remember", "bug_pattern", "-F", f"name={BUG['name']}",
              "-F", f"signature={BUG['signature']}")
    conv = cli(repo, "remember", "convention", "-F", "category=naming",
               "-F", "rule=webhook retry handlers live in webhooks/retry.py")
    guarded = cli(repo, "guard", "retry unbounded on the external webhook", json_out=False)
    assert f"- [{bug['id']}] [unreviewed] " in guarded
    recalled = cli(repo, "recall", "webhook retry handlers", json_out=False)
    assert f"- [{bug['id']}] [unreviewed] " in recalled
    assert f"- [{conv['id']}] [unreviewed]" not in recalled and f"- [{conv['id']}] " in recalled
    as_json = cli(repo, "guard", "retry unbounded on the external webhook")
    assert as_json["note"] == UNREVIEWED_NOTE and "[unreviewed]" not in as_json["prompt"]


# -- what agents are told -------------------------------------------------------------

def test_the_protocol_says_unreviewed_warnings_need_checking():
    for mode in ("mcp", "cli", "both"):
        assert "unreviewed" in block(mode)


def test_the_check_tool_description_mentions_unreviewed(repo):
    pytest.importorskip("mcp")
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session

    from agent_memory.mcp_server import build_server
    service = MemoryService(resolve_config(repo=str(repo), env={}))

    async def scenario():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(scenario())}
    service.close()
    assert "unreviewed" in tools["memory_check"].description
