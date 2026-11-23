"""Structure checks for the benchmark's worlds and tasks. None of these call recall."""
import hashlib
import re

import pytest

from agent_memory.record import KINDS
from agent_memory.retrieval.tokenize import tokenize

from tests.eval.bench import model
from tests.eval.bench.build import build_world

PRESENT = [w for w in model.WORLDS if (model.WORLD_DIR / f"{w}.json").exists()]
GENERIC = ["process", "handle", "document", "runner", "export", "import", "set", "write",
           "error", "test", "config", "log", "schema", "update", "migrate", "queue"]


@pytest.fixture(scope="module")
def worlds():
    return {name: model.load_world(name) for name in PRESENT}


@pytest.mark.parametrize("name", PRESENT)
def test_world_size_and_shape(worlds, name):
    w = worlds[name]
    kinds = {r.kind for r in w.records}
    assert 80 <= len(w.records) <= 110, len(w.records)
    assert len(kinds) >= 12, sorted(kinds)
    assert 12 <= len(w.files) <= 25, len(w.files)
    assert len(w.inactive) >= 6, "needs at least six superseded or retracted records"
    assert sum(1 for r in w.records if r.links) >= 8
    assert 1 <= len(w.pinned) <= 2
    assert sum(1 for r in w.records if r.anchors) >= 15
    scoped = sum(1 for r in w.records if r.anchors or r.scope or KINDS[r.kind].scope_field)
    assert scoped >= 0.4 * len(w.records), scoped


@pytest.mark.parametrize("name", PRESENT)
def test_world_builds_without_merges(worlds, name, tmp_path):
    built = build_world(worlds[name], tmp_path)
    assert len(built.ids) == len(worlds[name].records)
    built.memory.close() if hasattr(built.memory, "close") else None


@pytest.mark.parametrize("name", PRESENT)
def test_world_texts_are_not_near_duplicates(worlds, name):
    seen = {}
    for r in worlds[name].records:
        text = " ".join(str(v) for v in r.fields.values()).lower()
        assert text not in seen, f"{r.key} repeats {seen[text]}"
        seen[text] = r.key


def test_generic_words_recur_across_worlds(worlds):
    """The worlds must share ordinary vocabulary, or document frequency means nothing."""
    assert set(worlds) == set(model.WORLDS), "all three worlds must exist"
    stems = {t[0] for t in (tokenize(word) for word in GENERIC) if t}
    reused = 0
    for stem in stems:
        in_worlds = 0
        for w in worlds.values():
            hits = sum(1 for r in w.records
                       if stem in tokenize(" ".join(str(v) for v in r.fields.values())))
            in_worlds += hits >= 2
        reused += in_worlds >= 2
    assert reused >= 10, f"only {reused} of {len(stems)} generic words recur in two worlds"


SPLIT_FILES = sorted(p.name[:-5] for p in model.TASK_DIR.glob("*-*.json")) if model.TASK_DIR.exists() else []
TASK_SETS = [(n.split("-")[0], n.split("-")[1]) for n in SPLIT_FILES]       # (split, world)
SIZE = {"dev": (32, 38), "test": (62, 72)}


@pytest.fixture(scope="module")
def tasks():
    return {split: model.load_tasks(split) for split in model.SPLITS}


@pytest.mark.parametrize("split,name", TASK_SETS)
def test_task_set_shape(tasks, split, name):
    mine = [t for t in tasks[split] if t.world == name]
    low, high = SIZE[split]
    assert low <= len(mine) <= high, len(mine)
    empty = sum(1 for t in mine if t.empty)
    assert 0.28 <= empty / len(mine) <= 0.40, f"{empty}/{len(mine)} empty"
    for t in mine:
        if not t.empty:
            assert 1 <= len(t.relevant) <= 4, t
        assert len(t.acceptable) <= 6, t
    assert sum(1 for t in mine if t.files) >= 0.5 * len(mine)
    assert len({t.task.strip().lower() for t in mine if t.task.strip()}) == sum(
        1 for t in mine if t.task.strip()), "duplicate task text"


def test_no_task_text_is_shared_between_splits(tasks):
    dev = {t.task.strip().lower() for t in tasks["dev"] if t.task.strip()}
    test = {t.task.strip().lower() for t in tasks["test"] if t.task.strip()}
    assert not dev & test


def _digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_locked_test_split_is_unchanged():
    """The test split is locked. An edit shows up here and must be argued for in review;
    a wrong label is recorded in bench/RESULTS.md, not fixed in place."""
    lock = model.TASK_DIR / "test.sha256"
    assert lock.exists(), "tasks/test.sha256 is missing"
    expected = dict(line.split()[::-1] for line in lock.read_text().splitlines() if line.strip())
    files = sorted(model.TASK_DIR.glob("test-*.json"))
    assert sorted(expected) == [p.name for p in files]
    for path in files:
        assert _digest(path) == expected[path.name], f"{path.name} changed after it was locked"


BENCH_LOCKED = ["worlds/saas.json", "worlds/pipeline.json", "worlds/mobile.json",
                "model.py", "judge.py", "build.py", "stats.py"]


def test_benchmark_definition_files_are_unchanged():
    """The worlds, the loader, the judge, the builder and the statistics define what the locked
    labels mean. Changing one changes the benchmark: if it must change, update bench.sha256 and
    record the change in tests/eval/bench/RESULTS.md under "Label corrections / world changes"."""
    lock = model.BENCH / "bench.sha256"
    assert lock.exists(), "tests/eval/bench/bench.sha256 is missing"
    expected = {}
    for line in lock.read_text().splitlines():
        if line.strip():
            digest, name = line.split(None, 1)
            expected[name.strip()] = digest
    assert sorted(expected) == sorted(BENCH_LOCKED), (
        "the locked file list differs; record the change in tests/eval/bench/RESULTS.md under "
        '"Label corrections / world changes"')
    for name in BENCH_LOCKED:
        assert _digest(model.BENCH / name) == expected[name], (
            f"{name} changed after the benchmark was locked; record the change in "
            'tests/eval/bench/RESULTS.md under "Label corrections / world changes" and update bench.sha256')
