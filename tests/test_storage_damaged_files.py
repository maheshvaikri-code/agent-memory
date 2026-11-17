"""A damaged journal header or a missing snapshot is reported, never mistaken for an absent
journal (final review I2 and M2). In both cases acknowledged writes sit in the journal, and
treating it as absent would rewrite it to a fresh header and lose them."""
import os

import pytest

from agent_memory import AgentMemory
from agent_memory.storage import JournalBackend, journal
from agent_memory.storage.errors import StoreCorrupt


def _unfolded_writes(path, count=2):
    """A store whose journal holds `count` acknowledged writes not yet in the snapshot. The
    instance is not closed: close() would fold the journal into the snapshot."""
    memory = AgentMemory.open(path, agent="w")
    for i in range(count):
        memory.remember("convention", {"name": f"n{i}", "rule": f"acknowledged rule {i}"})
    return memory


def _rules(memory):
    return sorted(n.properties.get("rule") for n in memory.graph.nodes("convention"))


def _damage_first_byte(journal_path):
    data = open(journal_path, "rb").read()
    damaged = b"X" + data[1:]                       # one byte of the header changed
    open(journal_path, "wb").write(damaged)
    return damaged


def test_open_refuses_a_damaged_header_with_acknowledged_lines_behind_it(tmp_path):
    path = str(tmp_path / "p.memory")
    _unfolded_writes(path)
    damaged = _damage_first_byte(path + ".journal")
    with pytest.raises(StoreCorrupt, match="line 1"):
        AgentMemory.open(path)
    assert open(path + ".journal", "rb").read() == damaged       # not reset over it


def test_load_and_read_only_refuse_a_damaged_header(tmp_path):
    path = str(tmp_path / "p.memory")
    _unfolded_writes(path)
    damaged = _damage_first_byte(path + ".journal")
    with pytest.raises(StoreCorrupt):
        AgentMemory.load(path)
    with pytest.raises(StoreCorrupt):
        JournalBackend.read_only(path)
    assert open(path + ".journal", "rb").read() == damaged


def test_a_live_instance_does_not_reset_a_journal_whose_header_was_damaged(tmp_path):
    path = str(tmp_path / "p.memory")
    writer = _unfolded_writes(path)
    reader = AgentMemory.open(path, agent="r")
    assert _rules(reader) == ["acknowledged rule 0", "acknowledged rule 1"]
    writer.remember("convention", {"name": "n2", "rule": "acknowledged rule 2"})
    damaged = _damage_first_byte(path + ".journal")
    with pytest.raises(StoreCorrupt):
        reader.recall(task="acknowledged rule")      # lockless read retries under the lock
    with pytest.raises(StoreCorrupt):
        reader.remember("convention", {"name": "x", "rule": "must not be appended"})
    assert open(path + ".journal", "rb").read() == damaged


def test_a_header_line_that_is_complete_but_not_a_header_is_damage(tmp_path):
    path = tmp_path / "j"
    path.write_bytes(b"not a header\n")
    tail = journal.read_tail(path, 0)
    assert tail.generation is None and tail.error and "line 1" in tail.error


@pytest.mark.parametrize("content", [b"", b'{"journal":1,"gene'])
def test_an_empty_or_unfinished_header_is_still_an_absent_journal(tmp_path, content):
    path = str(tmp_path / "p.memory")
    _unfolded_writes(path).close()                  # everything folded into the snapshot
    open(path + ".journal", "wb").write(content)     # a crash while the header was written
    assert journal.read_tail(path + ".journal", 0).error is None
    memory = AgentMemory.open(path)
    assert _rules(memory) == ["acknowledged rule 0", "acknowledged rule 1"]
    memory.remember("convention", {"name": "n9", "rule": "after recovery"})
    memory.close()
    assert "after recovery" in _rules(AgentMemory.load(path))


def test_open_refuses_a_journal_with_transactions_but_no_snapshot(tmp_path):
    path = str(tmp_path / "p.memory")
    _unfolded_writes(path)
    os.unlink(path)
    journal_bytes = open(path + ".journal", "rb").read()
    with pytest.raises(StoreCorrupt, match="without its snapshot"):
        AgentMemory.open(path)
    assert not os.path.exists(path)
    assert open(path + ".journal", "rb").read() == journal_bytes
    with pytest.raises(FileNotFoundError):
        JournalBackend.read_only(path)


def test_open_creates_a_fresh_store_beside_a_header_only_journal(tmp_path):
    path = str(tmp_path / "p.memory")
    AgentMemory.open(path).close()                  # a header and nothing else
    os.unlink(path)
    memory = AgentMemory.open(path)
    assert _rules(memory) == []
    memory.remember("convention", {"name": "n", "rule": "fresh"})
    memory.close()
    assert _rules(AgentMemory.load(path)) == ["fresh"]
