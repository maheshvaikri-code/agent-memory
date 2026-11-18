"""A developer's own AGENT_MEMORY_* settings must never reach the suite: with
AGENT_MEMORY_STORE set, CLI tests that pass only --repo would otherwise write into the
developer's real store (resolve_config puts the environment before the default)."""
import os

from agent_memory.service import resolve_config

ISOLATED = ("AGENT_MEMORY_STORE", "AGENT_MEMORY_REPO", "AGENT_MEMORY_AGENT",
            "AGENT_MEMORY_FAULT", "AGENT_MEMORY_FAULT_AFTER")


def test_the_developers_agent_memory_settings_are_not_visible():
    assert [name for name in ISOLATED if name in os.environ] == []


def test_a_test_may_still_set_them_itself(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MEMORY_STORE", str(tmp_path / "mine.memory"))
    assert resolve_config(repo=str(tmp_path)).store_path == str(tmp_path / "mine.memory")
