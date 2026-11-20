"""3c cleanup: a lesson matching a retracted note is labelled, `files`/`symbols` cannot be field
names, and the label is the memory's own (an agent cannot set or hide it)."""
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


# -- rejected before ----------------------------------------------------------

def test_a_lesson_matching_a_rejected_lesson_is_queued_and_labelled(svc):
    first = svc.capture([good()])["captured"][0]
    svc.review_reject(first, "not true")
    out = svc.capture([good()])
    again = out["captured"][0]
    assert out["rejected_before"] == [again]
    entry = svc.review_show(again)
    assert first in entry["rejected_before"]            # which note, and when
    assert svc.review_list()["pending"][0]["rejected_before"] == entry["rejected_before"]


def test_a_lesson_matching_a_retracted_active_note_is_labelled_too(svc):
    made = svc.remember("quirk", {"description": TEXT})["id"]
    svc.retract(made, "wrong")
    out = svc.capture([good()])
    assert out["rejected_before"] == out["captured"]
    assert made in svc.review_show(out["captured"][0])["rejected_before"]


def test_a_lesson_that_matches_nothing_retracted_has_no_label(svc):
    out = svc.capture([good()])
    assert "rejected_before" not in out
    assert "rejected_before" not in svc.review_show(out["captured"][0])
    assert "rejected_before" not in svc.review_list()["pending"][0]


def test_only_the_matching_lessons_of_a_call_are_labelled(svc):
    first = svc.capture([good()])["captured"][0]
    svc.review_reject(first)
    out = svc.capture([good(), good("an unrelated lesson about the queue")])
    assert len(out["captured"]) == 2 and out["rejected_before"] == [out["captured"][0]]


def test_a_superseded_note_is_not_a_rejection(svc):
    old = svc.remember("quirk", {"description": "old wording of the zorblax fact"})["id"]
    svc.supersede(old, "quirk", {"description": "new wording of the zorblax fact"})
    out = svc.capture([good("old wording of the zorblax fact")])
    assert "rejected_before" not in out


def test_an_agent_cannot_set_or_forge_the_label(svc):
    out = svc.capture([good(fields={"description": TEXT, "rejected_before": "x on y"})])
    assert out["captured"] == [] and "rejected_before" in out["skipped"][0]["reason"]
    out = svc.capture([good()])
    assert "rejected_before" not in svc.review_show(out["captured"][0])


def test_the_label_names_the_most_recent_rejection(svc):
    first = svc.capture([good()])["captured"][0]
    svc.review_reject(first)
    second = svc.capture([good()])["captured"][0]
    svc.review_reject(second)
    third = svc.capture([good()])["captured"][0]
    assert second in svc.review_show(third)["rejected_before"]


def test_an_accepted_lesson_keeps_working_and_is_no_longer_pending(svc):
    first = svc.capture([good()])["captured"][0]
    svc.review_reject(first)
    again = svc.capture([good()])["captured"][0]
    svc.review_accept(again)
    assert [i["id"] for i in svc.recall("zorblax queue drops items")["items"]] == [again]


# -- reserved names -----------------------------------------------------------

@pytest.mark.parametrize("name", ["files", "symbols"])
def test_files_and_symbols_cannot_be_lesson_field_names(svc, name):
    out = svc.capture([good(fields={"description": TEXT, name: "src/cache.py"})])
    assert out["captured"] == [] and name in out["skipped"][0]["reason"]


@pytest.mark.parametrize("name", ["files", "symbols"])
def test_files_and_symbols_cannot_be_note_field_names_either(svc, name):
    with pytest.raises(ServiceError) as excinfo:
        svc.remember("quirk", {"description": TEXT, name: "x"})
    assert excinfo.value.code == "invalid_argument"
