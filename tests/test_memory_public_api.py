from agent_memory import AgentMemory
from agent_memory.storage import journal


def test_warnings_is_empty_for_a_plain_memory():
    assert AgentMemory("t").warnings == []


def test_warnings_include_storage_notes_without_duplicates(tmp_path):
    mem = AgentMemory.open(str(tmp_path / "p.memory"))
    mem._backend.warnings.append("compaction skipped: test")
    mem.load_warnings.append("load note")
    assert mem.warnings == ["load note", "compaction skipped: test"]
    mem._backend.warnings.append("load note")
    assert mem.warnings.count("load note") == 1
    mem.warnings.append("changing the copy changes nothing")
    assert "changing the copy changes nothing" not in mem.warnings


def test_carried_items_come_from_a_session_that_was_never_ended(tmp_path):
    path = str(tmp_path / "p.memory")
    a = AgentMemory.open(path, agent="a")
    sid = a.start_session("add retries")
    a.remember("open_question", {"question": "should retries be capped?"})
    a.remember("convention", {"category": "c", "rule": "not an open item"})
    b = AgentMemory.open(path, agent="b")            # a fresh process with no session of its own
    texts = [i.text for i in b.carried_items()]
    assert any("retries be capped" in t for t in texts)
    assert not any("not an open item" in t for t in texts)
    a.end_session(sid)
    assert b.carried_items() == []                   # a cleanly ended session is not carried


def test_unfolded_transactions_counts_what_the_snapshot_lacks(tmp_path):
    plain = AgentMemory("t")
    assert plain.unfolded_transactions() == 0
    mem = AgentMemory.open(str(tmp_path / "p.memory"))
    base = mem.unfolded_transactions()
    mem.remember("convention", {"category": "c", "rule": "one"})
    mem.remember("convention", {"category": "c", "rule": "two"})
    assert mem.unfolded_transactions() == base + 2
    assert mem.checkpoint() is True
    assert mem.unfolded_transactions() == 0


def test_another_process_writes_count_as_unfolded_too(tmp_path):
    path = str(tmp_path / "p.memory")
    a = AgentMemory.open(path)
    b = AgentMemory.open(path)
    b.remember("convention", {"category": "c", "rule": "from b"})
    assert a.unfolded_transactions() >= 1            # a syncs first, so it sees b's transaction


def test_load_returns_replay_warnings(tmp_path):
    path = str(tmp_path / "p.memory")
    mem = AgentMemory.open(path)
    mem.remember("convention", {"category": "c", "rule": "one"})
    seq = mem._backend.seq
    # a transaction that adds a node which already exists: replay applies it idempotently and notes it
    node = next(iter(mem.graph.nodes("convention")))
    op = journal.encode_op(["n", "convention", node.id, {"rule": "dup"}])
    with open(path + ".journal", "ab") as handle:
        handle.write(journal.encode_tx(seq + 1, "x", "2026-11-17T00:00:00+00:00", [op]))
    loaded = AgentMemory.load(path)
    assert any("already existed" in w for w in loaded.load_warnings)
    assert any("already existed" in w for w in loaded.warnings)
