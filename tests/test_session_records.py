"""Session records: re-asking, re-recording, answering and carrying across sessions."""
from agent_memory import AgentMemory


def props(m, kind, node_id):
    return m.graph.get_node(kind, node_id).properties


# -- finding 1: an answered or resolved record is not a merge target -------------------

def test_reasking_an_answered_question_in_a_new_session_is_a_new_open_question():
    m = AgentMemory("t")
    s1 = m.start_session("first")
    first = m.sessions.add_open_question(s1, "Should webhook retries use jitter?")
    m.sessions.answer_question("Should webhook retries use jitter?", "yes", s1)
    m.end_session(s1)
    s2 = m.start_session("second")
    again = m.sessions.add_open_question(s2, "Should webhook retries use jitter?")
    assert again != first
    assert props(m, "open_question", again)["answered_in"] == ""
    assert props(m, "open_question", again)["session_id"] == s2
    assert props(m, "open_question", first)["answer"] == "yes"
    assert again in [i.record.id for i in m.recall("webhook retries jitter").items]


def test_reraising_a_resolved_blocker_is_a_new_blocker():
    m = AgentMemory("t")
    s1 = m.start_session("first")
    first = m.sessions.add_blocker(s1, "staging database is down")
    m.sessions.resolve_blocker("staging database is down", s1, "restarted")
    s2 = m.start_session("second")
    again = m.sessions.add_blocker(s2, "staging database is down")
    assert again != first
    assert props(m, "blocker", again)["resolved_in"] == ""
    assert again in [i.record.id for i in m.recall("staging database").items]


def test_rerecorded_partial_work_moves_to_the_new_session_and_is_carried():
    m = AgentMemory("t")
    s1 = m.start_session("one")
    pw1 = m.sessions.add_partial_work(s1, "refactor", "src/api.py", "half done")
    m.end_session(s1)
    s2 = m.start_session("two")
    pw2 = m.sessions.add_partial_work(s2, "refactor", "src/api.py", "half done")
    assert pw2 == pw1                                  # same open item, merged
    assert props(m, "partial_work", pw2)["session_id"] == s2
    carried = [i.record.id for i in m.recall("unrelated zebra task").items]
    assert pw2 in carried


def test_reasking_an_unanswered_question_still_merges():
    m = AgentMemory("t")
    s1 = m.start_session("first")
    a = m.sessions.add_open_question(s1, "max batch size?")
    s2 = m.start_session("second")
    b = m.sessions.add_open_question(s2, "max batch size?")
    assert a == b
    assert props(m, "open_question", b)["session_id"] == s2
