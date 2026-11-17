from agent_memory import AgentMemory
from agent_memory.stores.sessions import SessionStore

FIXED_NOW = "2026-11-17T10:00:00.000000"


def test_sessions_from_two_processes_in_one_second_get_distinct_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(SessionStore, "_now", lambda self: FIXED_NOW)   # the same second
    path = str(tmp_path / "p.memory")
    a, b = AgentMemory.open(path, agent="a"), AgentMemory.open(path, agent="b")
    first = a.start_session("first")
    monkeypatch.setattr(SessionStore, "_session_counter", 0)   # another process: its own counter
    second = b.start_session("second")
    assert first != second
    for mem in (a, b):
        mem.refresh()
        assert mem.graph.has_node("session", first) and mem.graph.has_node("session", second)
    assert a.graph.get_node("session", second).properties["task"] == "second"


def test_plain_sessions_started_quickly_get_distinct_ids():
    m = AgentMemory("p")
    first, second = m.start_session("one"), m.start_session("two")
    assert first != second and first.startswith("s_") and second.startswith("s_")
    assert m.graph.has_node("session", first) and m.graph.has_node("session", second)


def test_export_json_includes_other_processes_writes(tmp_path):
    path = str(tmp_path / "p.memory")
    a, b = AgentMemory.open(path, agent="a"), AgentMemory.open(path, agent="b")
    a.recall(task="anything")                       # a is in sync before b writes
    b.remember("convention", {"name": "late", "rule": "written by the other process"})
    out = tmp_path / "out.json"
    a.export_json(str(out))
    assert "written by the other process" in out.read_text(encoding="utf-8")
