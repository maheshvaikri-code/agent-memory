import pytest

from agent_memory.service import MemoryService, ServiceError, resolve_config

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


def capture_one(svc, text=TEXT, **change):
    return svc.capture([good(text, **change)])["captured"][0]


def code_of(excinfo):
    return excinfo.value.code


# -- list and show -----------------------------------------------------------

def test_list_shows_pending_lessons_oldest_first_with_limit(svc):
    first = capture_one(svc, "first lesson text")
    second = capture_one(svc, "second lesson text")
    out = svc.review_list()
    assert [e["id"] for e in out["pending"]] == [first, second]
    assert out["limit"] == 100 and out["expired"] == 0
    assert set(out["pending"][0]) == {"id", "kind", "text", "agent", "session_id",
                                      "created_at", "expired"}


def test_show_gives_the_whole_lesson(svc):
    ident = capture_one(svc, evidence="the reviewer reads this")
    shown = svc.review_show(ident)
    assert shown["kind"] == "quirk" and shown["agent"] == "tester"
    assert shown["fields"] == {"description": TEXT}
    assert shown["evidence"] == "the reviewer reads this"
    assert shown["files"] == ["src/cache.py"]
    assert shown["source"] == "extracted" and shown["confidence"] == 0.5


def test_show_refuses_an_unknown_id_and_a_record_that_is_not_pending(svc):
    with pytest.raises(ServiceError) as unknown:
        svc.review_show("quirk_nothing_000000")
    assert code_of(unknown) == "not_found"
    active = svc.remember("quirk", {"description": "an active note"})["id"]
    with pytest.raises(ServiceError) as active_err:
        svc.review_show(active)
    assert code_of(active_err) == "invalid_argument"


def test_recorded_text_is_neutralised_in_list_and_show(svc):
    ident = capture_one(svc, "use <b>bold</b> \x1b[31mred\x1b[0m tags", evidence="<i>why</i>\x07")
    listed = svc.review_list()["pending"][0]
    shown = svc.review_show(ident)
    for text in (listed["text"], shown["evidence"], shown["fields"]["description"]):
        assert "<" not in text and "\x1b" not in text and "\x07" not in text


def test_list_and_show_work_on_a_read_only_service(repo, svc):
    ident = capture_one(svc)
    svc.close()
    ro = MemoryService(resolve_config(repo=str(repo), env={}, read_only=True))
    assert [e["id"] for e in ro.review_list()["pending"]] == [ident]
    assert ro.review_show(ident)["kind"] == "quirk"
    ro.close()


def test_an_expired_lesson_is_flagged_in_the_list(svc):
    ident = capture_one(svc)
    svc.memory.graph.update_node("quirk", ident, created_at="2020-01-01T00:00:00+00:00")
    out = svc.review_list()
    assert out["pending"][0]["expired"] is True and out["expired"] == 1


# -- accept ------------------------------------------------------------------

def test_accept_makes_the_lesson_recallable_and_reviewed(svc):
    ident = capture_one(svc)
    assert svc.review_accept(ident) == {"ok": True, "id": ident, "status": "active"}
    checked = svc.check("zorblax queue drops items over 64KB")
    assert [i["id"] for i in checked["items"]] == [ident]
    assert checked["items"][0]["unreviewed"] is False and checked["note"] == ""
    assert svc.review_list()["pending"] == []
    assert svc.status()["total_active"] == 1 and svc.status()["pending"] == 0


def test_accept_twice_is_refused(svc):
    ident = capture_one(svc)
    svc.review_accept(ident)
    with pytest.raises(ServiceError) as err:
        svc.review_accept(ident)
    assert code_of(err) == "invalid_argument"


def test_accept_refuses_when_an_active_note_now_has_the_same_text(svc):
    ident = capture_one(svc)
    svc.remember("quirk", {"description": TEXT})
    with pytest.raises(ServiceError) as err:
        svc.review_accept(ident)
    assert code_of(err) == "invalid_argument" and "same text" in err.value.message
    assert svc.review_show(ident)["kind"] == "quirk"            # still pending


def test_accept_refuses_a_lesson_that_expired_in_the_meantime(svc):
    ident = capture_one(svc)
    svc.memory.graph.update_node("quirk", ident, created_at="2020-01-01T00:00:00+00:00")
    with pytest.raises(ServiceError) as err:
        svc.review_accept(ident)
    assert code_of(err) == "invalid_argument" and "archived" in err.value.message


def test_accept_refuses_a_kind_agents_cannot_write(svc):
    result = svc.memory.remember("session_decision", {"decision": "x", "rationale": "y"},
                                 source="extracted", status="pending")
    with pytest.raises(ServiceError) as err:
        svc.review_accept(str(result.id))
    assert code_of(err) == "invalid_argument"


def test_a_read_only_service_refuses_accept_and_reject(repo, svc):
    ident = capture_one(svc)
    svc.close()
    ro = MemoryService(resolve_config(repo=str(repo), env={}, read_only=True))
    for call in (lambda: ro.review_accept(ident), lambda: ro.review_reject(ident)):
        with pytest.raises(ServiceError) as err:
            call()
        assert code_of(err) == "invalid_argument"
    ro.close()


# -- reject ------------------------------------------------------------------

def test_reject_retracts_the_lesson_and_keeps_the_reason(svc):
    ident = capture_one(svc)
    assert svc.review_reject(ident, "not true") == {"ok": True, "id": ident}
    node = svc.memory.graph.get_node("quirk", ident).properties
    assert node["status"] == "retracted" and "not true" in node["evidence"]
    assert svc.review_list()["pending"] == [] and svc.status()["total_active"] == 0
    assert svc.recall("zorblax queue drops items")["items"] == []


def test_reject_refuses_a_record_that_is_not_pending(svc):
    active = svc.remember("quirk", {"description": "an active note"})["id"]
    with pytest.raises(ServiceError) as err:
        svc.review_reject(active)
    assert code_of(err) == "invalid_argument"
