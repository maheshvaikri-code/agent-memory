"""Store lookups by name or text prefix resolve to the record the caller means.

The writer stores a record under a suffixed id when the name-built id is taken, so a
store method that rebuilds an id from a name must pick the record whose name or text
matches, and an "add or update" by natural key must supersede rather than duplicate.
"""
import pytest

from agent_memory import AgentMemory
from agent_memory.record import MemoryRecord


def status(m, kind, node_id):
    return MemoryRecord.from_node(m.graph.get_node(kind, node_id)).status


def active(m, kind):
    return [n.id for n in m.graph.nodes(kind) if status(m, kind, n.id) == "active"]


# -- name-keyed mutators ---------------------------------------------------------------

def test_answer_lands_on_the_question_with_that_text_not_the_first_with_its_prefix():
    m = AgentMemory("t")
    s = m.start_session("x")
    a = m.sessions.add_open_question(s, "How should we handle retries for the payment webhook")
    b = m.sessions.add_open_question(s, "How should we handle retries for the email sender")
    assert a != b
    m.sessions.answer_question("How should we handle retries for the email sender", "cap 3", s)
    assert m.graph.get_node("open_question", b).properties["answer"] == "cap 3"
    assert m.graph.get_node("open_question", a).properties["answer"] == ""


def test_answer_after_reasking_goes_to_the_open_question():
    m = AgentMemory("t")
    s1 = m.start_session("one")
    first = m.sessions.add_open_question(s1, "Should webhook retries use jitter?")
    m.sessions.answer_question("Should webhook retries use jitter?", "yes", s1)
    s2 = m.start_session("two")
    again = m.sessions.add_open_question(s2, "Should webhook retries use jitter?")
    m.sessions.answer_question("Should webhook retries use jitter?", "no, fixed backoff", s2)
    assert m.graph.get_node("open_question", first).properties["answer"] == "yes"
    assert m.graph.get_node("open_question", again).properties["answer"] == "no, fixed backoff"


def test_resolve_blocker_finds_the_blocker_with_that_text():
    m = AgentMemory("t")
    s = m.start_session("x")
    a = m.sessions.add_blocker(s, "Waiting on the platform team for staging access")
    b = m.sessions.add_blocker(s, "Waiting on the platform team for a new DNS record")
    m.sessions.resolve_blocker("Waiting on the platform team for a new DNS record", s, "done")
    assert m.graph.get_node("blocker", b).properties["resolved_in"] == s
    assert m.graph.get_node("blocker", a).properties["resolved_in"] == ""


def test_ambiguous_lookup_raises_instead_of_mutating_the_first():
    m = AgentMemory("t")
    for node_id in ("bl_staging_down", "bl_staging_down_abc123"):
        m.graph.add_node("blocker", node_id, session_id="s", description="staging down",
                         resolved_in="", resolution="")
    with pytest.raises(ValueError):
        m.sessions.resolve_blocker("staging down", "s", "fixed")
    assert all(not n.properties["resolved_in"] for n in m.graph.nodes("blocker"))


def test_tech_debt_locations_sharing_thirty_characters_stay_apart():
    m = AgentMemory("t")
    loc_a = "src/services/payments/handlers/refund.py"
    loc_b = "src/services/payments/handlers/charge.py"
    a = m.techdebt.add_debt(loc_a, "god function")
    b = m.techdebt.add_debt(loc_b, "no tests")
    assert a != b and set(active(m, "tech_debt")) == {a, b}
    attempt = m.techdebt.add_refactoring_attempt(loc_b, "split by type", "failed", "cycles")
    assert m.graph.has_edge("ATTEMPTED_ON", ("refactoring_attempt", attempt), ("tech_debt", b))
    assert m.techdebt.get_failed_attempts(loc_b) and not m.techdebt.get_failed_attempts(loc_a)


def test_quirk_and_workaround_lookups_use_the_full_text():
    m = AgentMemory("t")
    m.dependencies.add_dependency("rocksdb", "0.22", "storage")
    q1 = m.dependencies.add_quirk("rocksdb", "batch writes over 4MB stall the compaction thread")
    q2 = m.dependencies.add_quirk("rocksdb", "batch writes over 4MB stall the WAL fsync")
    assert q1 != q2
    w = m.dependencies.add_workaround("batch writes over 4MB stall the WAL fsync", "chunk into 2MB")
    assert m.graph.has_edge("WORKAROUND_FOR", ("workaround", w), ("quirk", q2))
    assert not m.graph.has_edge("WORKAROUND_FOR", ("workaround", w), ("quirk", q1))


# -- natural-key "add or update" -------------------------------------------------------

def test_readding_a_module_with_a_new_purpose_supersedes_the_old_one():
    m = AgentMemory("t")
    old = m.codebase.add_module("src/payments", "handles card payments via Stripe")
    new = m.codebase.add_module("src/payments", "handles card payments via Adyen (migrated off Stripe)")
    assert new != old
    assert active(m, "module") == [new]
    assert status(m, "module", old) == "superseded"
    assert m.graph.has_edge("SUPERSEDES", ("module", new), ("module", old))
    prompt = m.recall("payments provider", files=["src/payments/charge.py"]).to_prompt()
    assert "Adyen" in prompt and "via Stripe" not in prompt


def test_same_module_text_twice_still_merges():
    m = AgentMemory("t")
    a = m.codebase.add_module("src/cache", "LRU caching layer")
    b = m.codebase.add_module("src/cache", "LRU caching layer")
    assert a == b and active(m, "module") == [a]


def test_links_follow_the_record_that_replaced_a_module():
    m = AgentMemory("t")
    m.codebase.add_module("src/payments", "card payments via Stripe")
    m.decisions.add_decision("Use Postgres", "for billing ledger")
    m.decisions.add_constraint("Use Postgres", "src/payments", "never write raw SQL")
    new = m.codebase.add_module("src/payments", "card payments via Adyen")
    assert m.decisions.get_constraints_for_module("src/payments")
    assert m.codebase.get_module_context("src/payments")["module"]["purpose"] == "card payments via Adyen"
    m.codebase.update_confidence("src/payments", 0.9)
    assert m.graph.has_edge("MENTAL_MODEL_CONFIDENCE", ("agent", "self"), ("module", new))
    assert [w.record.id for w in m.check("use Postgres advisory locks",
                                         files=["src/payments/charge.py"])] == ["dec_use_postgres"]


def test_readding_a_decision_supersedes_it_and_later_links_reach_the_new_one():
    m = AgentMemory("t")
    old = m.decisions.add_decision("Use Postgres", "for billing ledger")
    new = m.decisions.add_decision("Use Postgres", "for billing ledger and the audit log")
    assert active(m, "decision") == [new] and status(m, "decision", old) == "superseded"
    alt = m.decisions.add_alternative("Use Postgres", "MongoDB", "no transactions")
    assert m.graph.has_edge("CONSIDERED_FOR", ("alternative", alt), ("decision", new))
    m.decisions.record_violation("Use Postgres", "use mongo", "kept postgres")
    assert m.graph.has_edge("AGENT_VIOLATED", ("agent", "self"), ("decision", new))


@pytest.mark.parametrize("add", [
    lambda m, v: m.performance.add_hot_path("node_lookup", v),
    lambda m, v: m.performance.add_constraint("node_lookup", v, "user experience"),
    lambda m, v: m.team.add_expertise("graph_algorithms", v),
    lambda m, v: m.codebase.add_pattern("singleton", ["src/cache"], why_chosen=v),
    lambda m, v: m.techdebt.add_debt("src/traverse.py", v),
])
def test_every_natural_key_kind_keeps_one_active_record(add):
    m = AgentMemory("t")
    first = add(m, "first value")
    second = add(m, "second value")
    kind = m.writer.find(second)[0]
    assert active(m, kind) == [second]
    assert status(m, kind, first) == "superseded"


def test_a_third_update_supersedes_the_current_record():
    m = AgentMemory("t")
    ids = [m.codebase.add_module("src/a", purpose) for purpose in ("one", "two", "three")]
    assert len(set(ids)) == 3 and active(m, "module") == [ids[2]]


def test_lookups_by_name_reach_the_replacing_record():
    m = AgentMemory("t")
    m.bugs.add_pattern("race_condition", "shared state without a lock")
    m.performance.add_hot_path("node_lookup", "high")
    hp = m.performance.add_hot_path("node_lookup", "very_high")
    assert m.performance._resolve("hot_path", "hp_node_lookup", name="node_lookup") == hp
    m.team.add_member("ana", "backend")
    m.team.add_expertise("graph_algorithms", "medium")
    ea = m.team.add_expertise("graph_algorithms", "deep")
    m.team.link_expertise("ana", "graph_algorithms")
    assert m.graph.has_edge("HAS_EXPERTISE", ("team_member", "tm_ana"), ("expertise_area", ea))
