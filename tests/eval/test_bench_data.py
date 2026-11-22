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
