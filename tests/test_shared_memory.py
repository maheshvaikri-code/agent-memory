import os

import pytest

from agent_memory import AgentMemory, StoreInUse


def test_open_creates_a_store_and_a_second_open_recalls_without_being_told(tmp_path):
    path = str(tmp_path / "p.memory")
    a = AgentMemory.open(path, agent="a")
    b = AgentMemory.open(path, agent="b")
    a.remember("convention", {"name": "naming", "rule": "cache keys use colon namespaces"})
    found = b.recall(task="how should I name cache keys")            # no refresh() call
    assert any("colon namespaces" in item.text for item in found.items)


def test_agent_self_node_exists_once_however_many_opens(tmp_path):
    path = str(tmp_path / "p.memory")
    for _ in range(3):
        AgentMemory.open(path)
    assert len(list(AgentMemory.open(path).graph.nodes("agent"))) == 1


def test_load_replays_the_journal(tmp_path):
    path = str(tmp_path / "p.memory")
    a = AgentMemory.open(path)
    a.remember("convention", {"name": "n", "rule": "unflushed rule"})
    loaded = AgentMemory.load(path)                           # nothing was checkpointed
    assert any("unflushed rule" in r.text for r in
               loaded.recall(task="unflushed rule").items)
    assert loaded.is_shared is False


def test_stats_do_not_count_the_meta_node(tmp_path):
    shared = AgentMemory.open(str(tmp_path / "p.memory"))
    plain = AgentMemory("p")
    assert shared.get_stats()["total_nodes"] == plain.get_stats()["total_nodes"]


def test_plain_save_over_a_live_store_is_refused_but_a_copy_is_fine(tmp_path):
    path = str(tmp_path / "p.memory")
    AgentMemory.open(path).remember("convention", {"name": "n", "rule": "r"})
    loaded = AgentMemory.load(path)
    with pytest.raises(StoreInUse):
        loaded.save(path)
    loaded.save(str(tmp_path / "copy.memory"))
    assert AgentMemory.load(str(tmp_path / "copy.memory")).get_stats()["total_nodes"] >= 1


def test_shared_save_to_its_own_path_is_a_checkpoint(tmp_path):
    path = str(tmp_path / "p.memory")
    mem = AgentMemory.open(path)
    mem.remember("convention", {"name": "n", "rule": "r"})
    assert os.path.getsize(path + ".journal") > 100
    mem.save(path)
    header_only = len(open(path + ".journal", "rb").read().splitlines()) == 1
    assert header_only
    mem.save(str(tmp_path / "copy.memory"))
    assert (tmp_path / "copy.memory").exists()


def test_close_and_context_manager(tmp_path):
    path = str(tmp_path / "p.memory")
    with AgentMemory.open(path) as mem:
        mem.remember("convention", {"name": "n", "rule": "r"})
    with pytest.raises(ValueError):
        mem.remember("convention", {"name": "n2", "rule": "r2"})
    assert AgentMemory.open(path).get_stats()["total_nodes"] >= 2


def test_options_are_forwarded_and_validated(tmp_path):
    AgentMemory.open(str(tmp_path / "a.memory"), durability="fsync", lock_timeout=2.0)
    with pytest.raises(ValueError):
        AgentMemory.open(str(tmp_path / "b.memory"), durability="sometimes")


def test_plain_memory_is_unchanged(tmp_path):
    mem = AgentMemory("t")
    assert mem.is_shared is False and mem.refresh() == 0 and mem.checkpoint() is False
    mem.close()                                              # harmless
    path = str(tmp_path / "p.memory")
    mem.remember("convention", {"name": "n", "rule": "r"})
    mem.save(path)
    assert AgentMemory.load(path).get_stats()["total_nodes"] == mem.get_stats()["total_nodes"]
