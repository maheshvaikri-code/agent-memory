import shutil

import pytest

from agent_memory import AgentMemory
from agent_memory.storage.errors import StoreCorrupt


def make_store(tmp_path, n=3):
    path = str(tmp_path / "p.memory")
    mem = AgentMemory.open(path)
    for i in range(n):
        mem.remember("convention", {"category": "c", "rule": f"rule number {i}"})
    return path, mem


def flip_generation_byte(journal_path):
    data = bytearray(open(journal_path, "rb").read())
    at = data.index(b'"generation":"') + len(b'"generation":"')
    data[at] = ord("0") if data[at] != ord("0") else ord("1")
    open(journal_path, "wb").write(bytes(data))


def test_a_damaged_generation_id_raises_instead_of_dropping_acknowledged_writes(tmp_path):
    path, _ = make_store(tmp_path)           # the journal holds the writes; the snapshot is behind
    journal_path = path + ".journal"
    flip_generation_byte(journal_path)
    before = open(journal_path, "rb").read()
    with pytest.raises(StoreCorrupt) as exc:
        AgentMemory.open(path)
    assert "seq" in str(exc.value)
    with pytest.raises(StoreCorrupt):
        AgentMemory.load(path)
    assert open(journal_path, "rb").read() == before    # nothing was reset or truncated


def test_a_genuinely_stale_journal_still_recovers(tmp_path):
    path, mem = make_store(tmp_path)
    journal_path = path + ".journal"
    old = open(journal_path, "rb").read()
    assert mem.checkpoint() is True
    open(journal_path, "wb").write(old)       # a compaction crashed before rewriting the journal
    again = AgentMemory.open(path)
    assert len(list(again.graph.nodes("convention"))) == 3
    assert again._backend.warnings == []


def test_a_snapshot_restored_from_an_older_backup_is_refused(tmp_path):
    path, mem = make_store(tmp_path, n=1)
    mem.checkpoint()
    backup = str(tmp_path / "backup.memory")
    shutil.copyfile(path, backup)             # generation G, small seq
    for i in range(2):
        mem.remember("convention", {"category": "c", "rule": f"later rule {i}"})
    assert mem.checkpoint() is True           # the snapshot moves to a new generation
    mem.remember("convention", {"category": "c", "rule": "after the compaction"})
    shutil.copyfile(backup, path)             # an operator restores the old snapshot
    with pytest.raises(StoreCorrupt):
        AgentMemory.open(path)


def test_a_live_instance_that_meets_a_damaged_header_raises_on_refresh(tmp_path):
    path, mem = make_store(tmp_path)
    flip_generation_byte(path + ".journal")
    with pytest.raises(StoreCorrupt):
        mem.refresh()


def test_an_undamaged_store_opens_normally(tmp_path):
    path, _ = make_store(tmp_path)
    again = AgentMemory.open(path)
    assert len(list(again.graph.nodes("convention"))) == 3
