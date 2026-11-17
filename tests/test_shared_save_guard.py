"""save() to a path that is another shared store is refused for shared instances too (final
review I5). Writing a snapshot over a store with a journal makes the next opener reset that
journal, and the other store's acknowledged writes are lost."""
import pytest

from agent_memory import AgentMemory, StoreInUse


def _rules(memory):
    return sorted(n.properties.get("rule") for n in memory.graph.nodes("convention"))


def test_shared_save_over_another_live_store_is_refused_and_leaves_it_intact(tmp_path):
    path_a, path_b = str(tmp_path / "a.memory"), str(tmp_path / "b.memory")
    a = AgentMemory.open(path_a)
    a.remember("convention", {"name": "a", "rule": "rule of A"})
    b = AgentMemory.open(path_b)
    b.remember("convention", {"name": "b", "rule": "rule of B, only in its journal"})
    snapshot, journal = (open(p, "rb").read() for p in (path_b, path_b + ".journal"))
    with pytest.raises(StoreInUse):
        a.save(path_b)
    assert open(path_b, "rb").read() == snapshot
    assert open(path_b + ".journal", "rb").read() == journal
    reopened = AgentMemory.open(path_b)
    assert _rules(reopened) == ["rule of B, only in its journal"]
    for memory in (a, b, reopened):
        memory.close()


def test_shared_save_to_a_fresh_path_still_writes_a_copy(tmp_path):
    a = AgentMemory.open(str(tmp_path / "a.memory"))
    a.remember("convention", {"name": "a", "rule": "rule of A"})
    copy = str(tmp_path / "copy.memory")
    a.save(copy)
    assert _rules(AgentMemory.load(copy)) == ["rule of A"]
    a.save(copy)                                     # overwriting a plain copy is fine
    a.close()


def test_shared_save_to_its_own_path_is_still_a_checkpoint(tmp_path):
    path = str(tmp_path / "a.memory")
    a = AgentMemory.open(path)
    a.remember("convention", {"name": "a", "rule": "rule of A"})
    a.save(path)
    assert open(path + ".journal", "rb").read().count(b"\n") == 1     # header only: folded
    a.close()


def test_plain_saves_are_unchanged(tmp_path):
    plain = AgentMemory("p")
    plain.remember("convention", {"name": "p", "rule": "plain rule"})
    path = str(tmp_path / "p.memory")
    plain.save(path)
    plain.save(path)                                 # over its own plain file
    shared = AgentMemory.open(str(tmp_path / "s.memory"))
    with pytest.raises(StoreInUse):
        plain.save(str(tmp_path / "s.memory"))
    assert _rules(AgentMemory.load(path)) == ["plain rule"]
    shared.close()
