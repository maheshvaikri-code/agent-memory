from datetime import datetime, timedelta, timezone

from agent_memory import AgentMemory
from agent_memory.record import MemoryRecord


def old(days):
    return (datetime.now() - timedelta(days=days)).isoformat()


def test_decay_archives_and_never_deletes():
    m = AgentMemory("t")
    m.graph.add_node("partial_work", "pw_old", session_id="s", work_type="fn",
                     location="a.py", state="half", added=old(30))
    summary = m._consolidator.consolidate()
    assert summary["decayed"] == 1
    assert m.graph.has_node("partial_work", "pw_old")
    assert MemoryRecord.from_node(m.graph.get_node("partial_work", "pw_old")).status == "archived"
    assert m._consolidator.consolidate()["decayed"] == 0


def test_an_edge_no_longer_makes_a_node_immortal(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    m = AgentMemory("t", repo_root=str(tmp_path))
    m.graph.add_node("partial_work", "pw_old", session_id="s", work_type="fn",
                     location="b.py", state="half", added=old(30))
    m.anchor("pw_old", "a.py")
    assert m._consolidator.consolidate()["decayed"] == 1


def test_exact_duplicates_are_merged_into_the_most_confident():
    m = AgentMemory("t")
    m.graph.add_node("convention", "c1", category="naming", rule="snake_case", confidence=0.6)
    m.graph.add_node("convention", "c2", category="naming", rule="Snake_Case", confidence=0.8,
                     evidence="lint config")
    summary = m._consolidator.consolidate()
    assert summary["merged"] == 1
    keep = MemoryRecord.from_node(m.graph.get_node("convention", "c2"))
    drop = MemoryRecord.from_node(m.graph.get_node("convention", "c1"))
    assert keep.status == "active" and drop.status == "superseded"
    assert m.graph.has_edge("SUPERSEDES", ("convention", "c2"), ("convention", "c1"))
    assert m._consolidator.consolidate()["merged"] == 0


def test_archived_records_are_not_recalled():
    m = AgentMemory("t")
    m.graph.add_node("open_question", "q_old", session_id="s", question="max batch size?",
                     answered_in="", answer="", added=old(60))
    m._consolidator.consolidate()
    assert m.recall(task="max batch size").items == []


def test_writer_record_decays_via_created_at():
    m = AgentMemory("t")
    res = m.writer.add("open_question", {"session_id": "s", "question": "q?",
                                         "answered_in": "", "answer": ""})
    aged = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    m.graph.update_node("open_question", res.id, created_at=aged)
    assert m._consolidator.consolidate()["decayed"] == 1
    assert MemoryRecord.from_node(m.graph.get_node("open_question", res.id)).status == "archived"
    assert m._consolidator.consolidate()["decayed"] == 0


def test_legacy_naive_added_still_decays():
    m = AgentMemory("t")
    m.graph.add_node("open_question", "q_legacy", session_id="s", question="legacy?",
                     answered_in="", answer="", added=old(60))
    assert m._consolidator.consolidate()["decayed"] == 1
    assert m._consolidator.consolidate()["decayed"] == 0


def test_decay_only_touches_active_records():
    m = AgentMemory("t")
    m.graph.add_node("partial_work", "pw_sup", session_id="s", work_type="fn",
                     location="a.py", state="half", added=old(30), status="superseded")
    assert m._consolidator.consolidate()["decayed"] == 0
    assert MemoryRecord.from_node(m.graph.get_node("partial_work", "pw_sup")).status == "superseded"
