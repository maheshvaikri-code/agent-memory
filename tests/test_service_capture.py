import pytest

from agent_memory import anchors as anchors_mod
from agent_memory.service import CODES, MemoryService, ServiceError, resolve_config
from agent_memory.service import service as service_module

TEXT = "the zorblax queue drops items over 64KB"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def svc(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="tester", env={}))
    yield service
    service.close()


def good(text=TEXT, **change):
    lesson = {"kind": "quirk", "fields": {"description": text}, "evidence": "seen in a test run",
              "files": ["src/cache.py"]}
    lesson.update(change)
    return lesson


def code_of(excinfo):
    return excinfo.value.code


def test_queue_full_is_a_service_error_code():
    assert "queue_full" in CODES


# -- the happy path ----------------------------------------------------------

def test_a_lesson_is_captured_as_a_pending_record_nobody_recalls(svc):
    out = svc.capture([good()])
    assert set(out) == {"captured", "skipped", "pending"}
    assert len(out["captured"]) == 1 and out["skipped"] == [] and out["pending"] == 1
    record = svc.memory.pending_records()[0]
    assert (record.source, record.confidence, record.agent, record.status) == (
        "extracted", 0.5, "tester", "pending")
    assert record.evidence == "seen in a test run"
    assert anchors_mod.anchor_paths(svc.memory.graph, record.ref) == ["src/cache.py"]
    assert svc.recall("zorblax queue drops items")["items"] == []
    assert svc.check("zorblax queue drops items over 64KB")["items"] == []


def test_a_lesson_with_only_symbols_is_captured_and_keeps_them_in_the_evidence(svc):
    out = svc.capture([good(files=[], symbols=["CacheClient.get", "evict"])])
    assert len(out["captured"]) == 1
    evidence = svc.memory.pending_records()[0].evidence
    assert evidence == "seen in a test run; symbols: CacheClient.get, evict"


# -- the gate: one test per rule --------------------------------------------

GATE = [
    ("no evidence", {"evidence": ""}, "evidence"),
    ("evidence not text", {"evidence": 5}, "evidence"),
    ("no anchor", {"files": [], "symbols": []}, "file or symbol"),
    ("kind agents cannot write", {"kind": "session_decision"}, "unknown_kind"),
    ("kind not text", {"kind": ["quirk"]}, "unknown_kind"),
    ("file outside the repo", {"files": ["../outside.py"]}, "path_outside_repo"),
    ("field too long", {"fields": {"description": "x" * 2001}}, "too_large"),
    ("required field missing", {"fields": {"note": "x"}}, "needs"),
    ("priority key", {"priority": "must"}, "unknown lesson key"),
    ("confidence key", {"confidence": 0.9}, "unknown lesson key"),
]


@pytest.mark.parametrize("label,change,expect", GATE, ids=[g[0] for g in GATE])
def test_each_gate_rule_skips_the_lesson_and_names_the_reason(svc, label, change, expect):
    out = svc.capture([good(**change)])
    assert out["captured"] == [] and out["skipped"][0]["index"] == 0
    assert expect in out["skipped"][0]["reason"]
    assert svc.status()["pending"] == 0


def test_a_lesson_that_is_not_an_object_is_skipped(svc):
    out = svc.capture(["just text", good()])
    assert len(out["captured"]) == 1 and out["skipped"][0]["index"] == 0


def test_skip_reasons_never_carry_markup(svc):
    out = svc.capture([good(**{"<i>x": 1})])
    assert "<" not in out["skipped"][0]["reason"]


def test_a_bad_lesson_does_not_lose_the_good_ones(svc):
    out = svc.capture([good(evidence=""), good("another real lesson")])
    assert len(out["captured"]) == 1
    assert [s["index"] for s in out["skipped"]] == [0]
    assert svc.status()["pending"] == 1


def test_call_size_rules(svc):
    with pytest.raises(ServiceError) as too_many:
        svc.capture([good(f"lesson {i}") for i in range(11)])
    assert code_of(too_many) == "too_large"
    for bad in ([], "text", None, {"kind": "quirk"}):
        with pytest.raises(ServiceError) as err:
            svc.capture(bad)
        assert code_of(err) == "invalid_argument"
    assert svc.status()["pending"] == 0


# -- duplicates --------------------------------------------------------------

def test_a_duplicate_of_an_active_note_is_skipped_and_the_note_is_untouched(svc):
    first = svc.remember("quirk", {"description": TEXT})
    node = svc.memory.graph.get_node("quirk", first["id"]).properties
    before = (node["confidence"], node["evidence"])
    out = svc.capture([good()])
    assert out["captured"] == [] and "duplicate" in out["skipped"][0]["reason"]
    assert "active note" in out["skipped"][0]["reason"]
    node = svc.memory.graph.get_node("quirk", first["id"]).properties
    assert (node["confidence"], node["evidence"]) == before


def test_the_same_lesson_twice_gives_one_pending_record(svc):
    svc.capture([good()])
    again = svc.capture([good(), good("a different lesson")])
    assert len(again["captured"]) == 1
    assert "already waiting" in again["skipped"][0]["reason"]
    assert svc.status()["pending"] == 2


# -- queue limit -------------------------------------------------------------

def test_a_full_queue_raises_queue_full_and_stores_nothing(svc, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_PENDING", 2)
    svc.capture([good("one lesson"), good("two lesson")])
    with pytest.raises(ServiceError) as err:
        svc.capture([good("three lesson")])
    assert code_of(err) == "queue_full"
    assert svc.status()["pending"] == 2


def test_partial_room_stores_what_fits_and_skips_the_rest_as_queue_full(svc, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_PENDING", 2)
    out = svc.capture([good("one lesson"), good("two lesson"), good("three lesson")])
    assert len(out["captured"]) == 2
    assert out["skipped"][0]["index"] == 2 and "queue_full" in out["skipped"][0]["reason"]


# -- read-only ---------------------------------------------------------------

def test_a_read_only_service_refuses_capture(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}, read_only=True))
    with pytest.raises(ServiceError) as err:
        service.capture([good()])
    assert code_of(err) == "invalid_argument"
    service.close()


# -- session end -------------------------------------------------------------

def test_session_end_captures_lessons_stamps_the_session_and_still_ends(svc):
    sid = svc.session_start("a task")["session_id"]
    out = svc.session_end("done", lessons=[good(), good(evidence="")])
    assert out["ok"] is True
    assert len(out["capture"]["captured"]) == 1 and out["capture"]["skipped"][0]["index"] == 1
    assert svc.memory.pending_records()[0].session_id == sid
    with pytest.raises(ServiceError) as err:
        svc.session_end()
    assert code_of(err) == "no_active_session"


def test_session_end_reports_a_full_queue_in_the_result_not_as_an_error(svc, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_PENDING", 1)
    svc.capture([good("filler lesson")])
    svc.session_start("a task")
    out = svc.session_end(lessons=[good()])
    assert out["ok"] is True and out["capture"]["error"]["code"] == "queue_full"


def test_session_end_without_lessons_has_no_capture_key(svc):
    svc.session_start("a task")
    assert "capture" not in svc.session_end("done")
    svc.session_start("again")
    assert "capture" not in svc.session_end("done", lessons=[])


def test_session_end_reports_malformed_lessons_in_the_result(svc):
    svc.session_start("a task")
    out = svc.session_end(lessons="not a list")
    assert out["ok"] is True and out["capture"]["error"]["code"] == "invalid_argument"


def test_capture_works_with_no_active_session(svc):
    svc.capture([good()])
    assert svc.memory.pending_records()[0].session_id == ""


# -- counts ------------------------------------------------------------------

def test_status_and_session_start_report_the_pending_count(svc):
    assert svc.status()["pending"] == 0 and svc.status()["pending_expired"] == 0
    svc.capture([good()])
    status = svc.status()
    assert status["pending"] == 1 and status["total_active"] == 0
    assert svc.session_start("t")["pending_review"] == 1
    assert svc.session_start("t")["pending_review"] == 1           # already active: same count


def test_an_expired_lesson_is_counted_apart_until_it_is_swept(svc):
    out = svc.capture([good()])
    ident = out["captured"][0]
    svc.memory.graph.update_node("quirk", ident, created_at="2020-01-01T00:00:00+00:00")
    status = svc.status()
    assert status["pending"] == 0 and status["pending_expired"] == 1
    svc.capture([good("a fresh lesson")])                          # the sweep
    assert svc.status()["pending_expired"] == 0
