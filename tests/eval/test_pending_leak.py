"""Pending lessons never reach recall or check, whatever they say, and the scorecard does not
move with 60+ of them in the store. A control proves the test could see a leak: the same lesson
becomes visible the moment a person accepts it."""
import pytest

from agent_memory.service import MemoryService

from .eval_service import service_fns
from .harness import evaluate
from .heldout import HELDOUT
from .run import build
from .scenarios import SCENARIOS

MARK = "pendingleakmarkzx"


def lesson_for(scenario):
    text = f"{MARK} {scenario.task}" if scenario.task else f"{MARK} {scenario.files[0]}"
    return {"kind": "quirk", "fields": {"description": text}, "evidence": "eval lesson",
            "files": scenario.files[:1] or ["README.md"]}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    memory = build(tmp_path_factory.mktemp("shopline-pending"))
    return MemoryService(memory=memory)


def spying(service, seen):
    recall_fn, check_fn = service_fns(service)

    def recall(task, files):
        out = recall_fn(task, files)
        seen.append(out)
        return out

    def check(approach, files):
        out = check_fn(approach, files)
        seen.append(out)
        return out

    return recall, check


def test_pending_lessons_do_not_leak_and_do_not_move_the_scorecard(world):
    before = evaluate(*service_fns(world))
    before_held = evaluate(*service_fns(world), scenarios=HELDOUT)

    lessons = [lesson_for(s) for s in SCENARIOS + HELDOUT if s.task or s.files]
    stored = 0
    for start in range(0, len(lessons), 10):
        stored += len(world.capture(lessons[start:start + 10])["captured"])
    assert stored >= 40 and world.status()["pending"] == stored

    seen = []
    after = evaluate(*spying(world, seen))
    after_held = evaluate(*spying(world, seen), scenarios=HELDOUT)
    assert (after.recall, after.leaks, after.abstention) == (before.recall, before.leaks,
                                                              before.abstention)
    assert after_held.abstention == before_held.abstention
    assert seen and not any(MARK in text for text in seen)

    # control: the same kind of lesson is found once a person accepts it
    scenario = SCENARIOS[0]
    wanted = f"{MARK} {scenario.task}"
    target = next(e["id"] for e in world.review_list()["pending"] if wanted in e["text"])
    world.review_accept(target)
    found = world.recall(scenario.task, scenario.files, max_tokens=2000)["prompt"]
    assert MARK in found
