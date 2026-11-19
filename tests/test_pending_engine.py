import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agent_memory import AgentMemory
from agent_memory.record import PENDING_TTL_DAYS, STATUSES, MemoryRecord, pending_expired
from agent_memory.service.shaping import is_unreviewed
from storage_helpers import SRC

QUIRK = {"description": "the zorblax queue drops items over 64KB"}


def lesson(text, **extra):
    base = {"kind": "quirk", "fields": {"description": text}, "anchors": ["src/a.py"],
            "evidence": "seen in a test run"}
    base.update(extra)
    return base


def rec(memory, ref):
    return MemoryRecord.from_node(memory.graph.get_node(*ref))


def pend(memory, kind="quirk", fields=None, **kw):
    return memory.remember(kind, fields or QUIRK, source="extracted", status="pending", **kw)


@pytest.fixture
def memory():
    return AgentMemory("t")


# -- status and source -------------------------------------------------------

def test_pending_is_a_known_status_and_loads_without_a_warning(memory):
    assert "pending" in STATUSES
    result = pend(memory)
    warnings = []
    record = MemoryRecord.from_node(memory.graph.get_node(*result.ref), warnings)
    assert (record.status, record.source, record.confidence) == ("pending", "extracted", 0.5)
    assert warnings == []


def test_add_refuses_a_status_other_than_active_or_pending(memory):
    with pytest.raises(ValueError, match="status must be"):
        memory.remember("quirk", QUIRK, status="archived")


def test_a_pending_record_is_never_recalled_checked_or_carried(memory):
    memory.start_session("t")
    pend(memory)
    pend(memory, "partial_work", {"work_type": "refactor", "location": "src/a.py",
                                  "state": "half done"})
    assert memory.recall(task="zorblax queue drops items").items == []
    assert memory.check("zorblax queue drops items over 64KB") == []
    assert list(memory.carried_items()) == []


# -- no side effects on active notes -----------------------------------------

def test_a_pending_add_does_not_touch_an_active_note_with_the_same_text(memory):
    active = memory.remember("quirk", QUIRK)
    before = rec(memory, active.ref)
    result = pend(memory, evidence="from a lesson")
    assert result.created and not result.merged and result.ref != active.ref
    after = rec(memory, active.ref)
    assert (after.confidence, after.evidence, after.status) == (before.confidence,
                                                                before.evidence, "active")


def test_a_pending_record_is_not_a_merge_target(memory):
    first = pend(memory)
    second = memory.remember("quirk", QUIRK)
    assert second.created and second.ref != first.ref
    assert rec(memory, first.ref).status == "pending"
    assert rec(memory, second.ref).status == "active"


# -- find_duplicate ----------------------------------------------------------

def test_find_duplicate_sees_active_and_pending_and_prefers_active(memory):
    assert memory.find_duplicate("quirk", QUIRK) is None
    first = pend(memory)
    assert memory.find_duplicate("quirk", QUIRK) == (first.ref, "pending")
    active = memory.remember("quirk", QUIRK)
    assert memory.find_duplicate("quirk", QUIRK) == (active.ref, "active")


def test_an_answered_question_is_not_a_duplicate(memory):
    memory.remember("open_question", {"question": "why is the queue slow?", "answered_in": "s1"})
    assert memory.find_duplicate("open_question", {"question": "why is the queue slow?"}) is None


# -- accept ------------------------------------------------------------------

def test_accept_makes_a_lesson_active_reviewed_and_recallable(memory):
    result = pend(memory)
    memory.accept(result.id)
    record = rec(memory, result.ref)
    assert (record.status, record.source, record.confidence) == ("active", "reviewed", 0.7)
    assert [i.record.id for i in memory.check("zorblax queue drops items over 64KB")] == [result.id]


def test_a_reviewed_guardrail_is_not_flagged_unreviewed_but_an_extracted_one_is(memory):
    result = pend(memory)
    assert is_unreviewed(rec(memory, result.ref)) is True       # extracted
    memory.accept(result.id)
    assert is_unreviewed(rec(memory, result.ref)) is False      # reviewed


def test_accept_refuses_a_record_that_is_not_pending(memory):
    active = memory.remember("quirk", QUIRK)
    with pytest.raises(ValueError, match="not pending"):
        memory.accept(active.id)


def test_accept_refuses_when_an_active_note_with_the_same_text_exists(memory):
    result = pend(memory)
    memory.remember("quirk", QUIRK)
    with pytest.raises(ValueError, match="same text"):
        memory.accept(result.id)
    assert rec(memory, result.ref).status == "pending"


# -- expiry ------------------------------------------------------------------

def test_pending_expired_is_a_pure_age_test():
    now = datetime(2026, 12, 31, tzinfo=timezone.utc)
    old = (now - timedelta(days=PENDING_TTL_DAYS + 1)).isoformat()
    young = (now - timedelta(days=PENDING_TTL_DAYS - 1)).isoformat()
    assert pending_expired(old, now=now) is True and pending_expired(young, now=now) is False
    assert pending_expired("", now=now) is False and pending_expired("garbage", now=now) is False


def test_expire_pending_archives_only_old_pending_records(memory):
    old_pending = pend(memory)
    active = memory.remember("quirk", {"description": "an unrelated active quirk"})
    later = datetime.now(timezone.utc) + timedelta(days=PENDING_TTL_DAYS - 1)
    assert memory.expire_pending(now=later) == 0
    later = datetime.now(timezone.utc) + timedelta(days=PENDING_TTL_DAYS + 1)
    assert memory.expire_pending(now=later) == 1
    assert rec(memory, old_pending.ref).status == "archived"
    assert rec(memory, active.ref).status == "active"
    far = datetime.now(timezone.utc) + timedelta(days=4000)
    assert memory.expire_pending(now=far) == 0            # active records never expire here


def test_pending_records_survive_a_reopen(tmp_path):
    path = str(tmp_path / "p.memory")
    with AgentMemory.open(path) as shared:
        pend(shared)
    with AgentMemory.open(path) as shared:
        assert [r.status for r in shared.pending_records()] == ["pending"]


# -- capture -----------------------------------------------------------------

def test_capture_stores_pending_records_and_reports_them(memory):
    out = memory.capture([lesson("first lesson text"), lesson("second lesson text")],
                         max_pending=100)
    assert [c["index"] for c in out["captured"]] == [0, 1]
    assert out["skipped"] == [] and out["full"] is False and out["pending"] == 2
    records = memory.pending_records()
    assert len(records) == 2 and all(r.status == "pending" and r.source == "extracted"
                                     for r in records)
    assert all(r.evidence == "seen in a test run" for r in records)


def test_capture_skips_a_lesson_repeated_in_the_same_call(memory):
    out = memory.capture([lesson("same text"), lesson("same text")], max_pending=100)
    assert len(out["captured"]) == 1
    assert out["skipped"] == [{"index": 1, "code": "duplicate", "of": None,
                               "of_status": "pending"}]
    assert len(memory.pending_records()) == 1


def test_capture_skips_a_duplicate_of_an_active_note_and_leaves_it_alone(memory):
    active = memory.remember("quirk", QUIRK)
    before = rec(memory, active.ref)
    out = memory.capture([lesson(QUIRK["description"])], max_pending=100)
    assert out["captured"] == []
    assert out["skipped"] == [{"index": 0, "code": "duplicate", "of": active.id,
                               "of_status": "active"}]
    after = rec(memory, active.ref)
    assert (after.confidence, after.evidence) == (before.confidence, before.evidence)


def test_capture_stores_as_many_as_fit_and_skips_the_rest(memory):
    out = memory.capture([lesson(f"lesson {i}") for i in range(3)], max_pending=2)
    assert len(out["captured"]) == 2
    assert out["skipped"] == [{"index": 2, "code": "queue_full", "of": None, "of_status": None}]


def test_capture_with_no_room_stores_nothing_and_says_full(memory):
    memory.capture([lesson("one"), lesson("two")], max_pending=2)
    out = memory.capture([lesson("three")], max_pending=2)
    assert out["full"] is True and out["captured"] == [] and len(memory.pending_records()) == 2


def test_capture_sweeps_expired_lessons_before_counting(memory):
    first = memory.capture([lesson("one"), lesson("two")], max_pending=2)
    ref = ("quirk", first["captured"][0]["id"])
    memory.graph.update_node(ref[0], ref[1], created_at="2020-01-01T00:00:00+00:00")
    out = memory.capture([lesson("three")], max_pending=2)
    assert out["swept"] == 1 and len(out["captured"]) == 1 and out["full"] is False
    assert rec(memory, ref).status == "archived"


WORKER = """
import sys
from agent_memory import AgentMemory
store, tag = sys.argv[1], sys.argv[2]
lessons = [{"kind": "quirk", "fields": {"description": tag + " lesson " + str(i)},
            "anchors": ["a.py"], "evidence": "e"} for i in range(10)]
with AgentMemory.open(store) as memory:
    memory.capture(lessons, max_pending=100)
"""


def test_two_processes_cannot_push_the_queue_past_its_limit(tmp_path):
    path = str(tmp_path / "p.memory")
    with AgentMemory.open(path) as shared:
        shared.capture([lesson(f"seed {i}") for i in range(95)], max_pending=100)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([SRC, env.get("PYTHONPATH", "")])
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, path, tag], env=env)
             for tag in ("alpha", "beta")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    with AgentMemory.open(path) as shared:
        assert len(shared.pending_records()) == 100


# -- review fixes: pending writes cannot rewrite active notes -----------------

def test_an_accepted_lesson_is_a_merge_target_for_the_next_active_add(memory):
    memory.remember("quirk", {"description": "some other active quirk"})   # index the kind
    result = pend(memory)
    memory.accept(result.id)
    again = memory.remember("quirk", QUIRK)
    assert again.merged and not again.created and again.ref == result.ref
    same = [r for r in memory.graph.nodes("quirk")
            if MemoryRecord.from_node(r).text == QUIRK["description"]
            and MemoryRecord.from_node(r).status == "active"]
    assert len(same) == 1


DECISION = {"title": "use sqlite", "context": "small local store"}


def test_a_pending_add_under_an_explicit_id_cannot_replace_an_active_note(memory):
    active = memory.remember("decision", DECISION)
    with pytest.raises(ValueError):
        memory.remember("decision", {"title": "use sqlite", "context": "a different context"},
                        node_id=active.id, status="pending")
    assert rec(memory, active.ref).status == "active"


def test_a_pending_supersede_is_refused_and_leaves_the_old_note_active(memory):
    active = memory.remember("decision", DECISION)
    with pytest.raises(ValueError):
        memory.supersede(active.id, "decision", {"title": "use postgres", "context": "scale"},
                         status="pending")
    assert rec(memory, active.ref).status == "active"


def test_a_pending_add_stays_pending_even_when_the_fields_carry_a_status(memory):
    result = memory.remember("decision", {"title": "use zorblax", "context": "queue drops items",
                                          "status": "accepted"},
                             source="extracted", status="pending")
    assert rec(memory, result.ref).status == "pending"
    assert memory.recall(task="use zorblax queue drops items").items == []
    assert memory.check("use zorblax queue drops items") == []


def test_capture_stores_a_lesson_whose_fields_carry_a_status_as_pending(memory):
    out = memory.capture([{"kind": "decision",
                           "fields": {"title": "use zorblax", "context": "queue drops items",
                                      "status": "accepted"},
                           "anchors": ["src/a.py"], "evidence": "e"}], max_pending=10)
    assert len(out["captured"]) == 1
    assert [r.status for r in memory.pending_records()] == ["pending"]
