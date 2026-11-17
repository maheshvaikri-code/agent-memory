import pytest

from agent_memory import AgentMemory
from agent_memory.record import MemoryRecord


def rec(memory, result):
    return MemoryRecord.from_node(memory.graph.get_node(*result.ref))


def test_remember_stamps_the_envelope():
    m = AgentMemory("t", agent="codex")
    m.set_actor("codex", "s_1")
    r = m.remember("bug_pattern", {"name": "storm", "signature": "unbounded retry"},
                   evidence="INC-1", scope=["services/payments/**"])
    record = rec(m, r)
    assert r.created and not r.merged
    assert (record.agent, record.session_id, record.source) == ("codex", "s_1", "explicit")
    assert record.evidence == "INC-1" and record.scope == ["services/payments/**"]
    assert record.confidence == 0.7 and record.status == "active" and record.created_at


def test_same_text_merges_and_reinforces():
    m = AgentMemory("t")
    a = m.remember("bug_pattern", {"name": "storm", "signature": "unbounded retry"}, evidence="INC-1")
    b = m.remember("bug_pattern", {"name": "Storm", "signature": "unbounded   retry"}, evidence="INC-2")
    assert b.ref == a.ref and b.merged and not b.created
    record = rec(m, a)
    assert record.confidence == pytest.approx(0.8)
    assert "INC-1" in record.evidence and "INC-2" in record.evidence
    assert len(list(m.graph.nodes("bug_pattern"))) == 1


def test_near_duplicates_are_written_and_reported():
    m = AgentMemory("t")
    a = m.remember("convention", {"category": "naming", "rule": "cache keys use colon separated namespaces"})
    b = m.remember("convention", {"category": "naming", "rule": "cache keys use colon separated namespace prefixes"})
    assert b.created and a.ref in b.similar


def test_unknown_kind_and_bad_priority_are_rejected():
    m = AgentMemory("t")
    with pytest.raises(ValueError, match="Valid kinds"):
        m.remember("gossip", {"text": "x"})
    with pytest.raises(ValueError, match="priority"):
        m.remember("quirk", {"description": "x"}, priority="urgent")
    with pytest.raises(ValueError, match="no text"):
        m.remember("quirk", {"description": ""})


def test_supersede_links_and_invalidates_the_old_record():
    m = AgentMemory("t")
    old = m.remember("decision", {"title": "Process refunds synchronously", "context": "launch"})
    new = m.supersede(old.id, "decision", {"title": "Queue refunds", "context": "timeouts"})
    assert rec(m, old).status == "superseded" and rec(m, old).invalid_at
    assert rec(m, new).status == "active"
    assert m.graph.has_edge("SUPERSEDES", new.ref, old.ref)


def test_retract_and_feedback():
    m = AgentMemory("t")
    r = m.remember("quirk", {"description": "pipeline drops commands"})
    m.feedback(r.id, True)
    m.feedback(r.id, False)
    m.feedback(r.id, False)
    assert (rec(m, r).helpful, rec(m, r).harmful) == (1, 2)
    m.retract(r.id, "could not reproduce")
    assert rec(m, r).status == "retracted" and "could not reproduce" in rec(m, r).evidence


def test_unknown_id_raises_key_error():
    m = AgentMemory("t")
    with pytest.raises(KeyError):
        m.retract("nope")


def test_anchor_and_verify(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    m = AgentMemory("t", repo_root=str(tmp_path))
    r = m.remember("bug_pattern", {"name": "p", "signature": "s"}, anchors=["a.py"])
    assert m.graph.has_edge("ANCHORED_TO", r.ref, ("file", "a.py"))
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert m.verify(r.id) == 1


def test_location_field_becomes_an_anchor_when_it_is_a_file(tmp_path):
    (tmp_path / "runner.py").write_text("pass\n", encoding="utf-8")
    m = AgentMemory("t", repo_root=str(tmp_path))
    r = m.remember("tech_debt", {"location": "runner.py", "description": "no lock"})
    assert m.graph.has_edge("ANCHORED_TO", r.ref, ("file", "runner.py"))


def test_reactivating_text_of_a_superseded_record_gets_a_new_id():
    m = AgentMemory("t")
    old = m.remember("decision", {"title": "A", "context": "x"})
    m.supersede(old.id, "decision", {"title": "B", "context": "y"})
    again = m.remember("decision", {"title": "A", "context": "x"})
    assert again.created and again.ref != old.ref


def test_save_and_load_keeps_actor_settings_and_reports_warnings(tmp_path):
    m = AgentMemory("t")
    m.remember("quirk", {"description": "pipeline drops commands"})
    m.graph.add_node("decision", "bad", title="T", confidence="high")
    path = str(tmp_path / "m.memory")
    m.save(path)
    loaded = AgentMemory.load(path, repo_root=str(tmp_path), agent="cursor")
    assert loaded.writer.agent == "cursor" and loaded.repo_root == str(tmp_path)
    assert loaded.conventions.writer is loaded.writer
    assert any("bad" in w for w in loaded.load_warnings)


def test_set_actor_keeps_the_session_unless_given():
    m = AgentMemory("t")
    sid = m.start_session("work")
    m.set_actor("codex")
    assert (m.writer.agent, m.writer.session_id) == ("codex", sid)
    m.set_actor("cursor", "s_other")
    assert m.writer.session_id == "s_other"
    m.set_actor("cursor", "")
    assert m.writer.session_id == ""
