"""A snapshot that cannot be replaced does not make every later write retry the compaction
inside the lock, and the skip is noted once (final review I6).

Each test opens a store that compacts once its journal passes 200 bytes, makes one large
write (about 2 KB) that crosses it, then small writes that together stay below the size the
journal had at the skipped attempt, so only the time limit could end the back-off."""
import os
import time

import pytest

from agent_memory.storage import JournalBackend
from agent_memory.storage import backend as backend_mod
from agent_memory.storage.atomic import atomic_write_text as real_atomic_write_text

BIG, SMALL = "y" * 2000, "s" * 40


@pytest.fixture
def obstructed(monkeypatch):
    """Snapshot writes fail, as if a third party held the file open, while state["blocked"]
    is true. Returns (the attempts made while blocked, state)."""
    attempts = []
    state = {"blocked": False}

    def write(path, text, **kwargs):
        if state["blocked"]:
            attempts.append(path)
            raise PermissionError(13, "the snapshot is held open by another program")
        return real_atomic_write_text(path, text, **kwargs)

    monkeypatch.setattr(backend_mod, "atomic_write_text", write)
    return attempts, state


def _store(tmp_path, **kw):
    backend = JournalBackend(tmp_path / "p.memory", compact_min_bytes=200, **kw)
    return backend, backend.open()


def _skips(backend):
    return [w for w in backend.warnings if "compaction skipped" in w]


def test_a_skipped_compaction_backs_off_instead_of_retrying_on_every_write(tmp_path,
                                                                           obstructed):
    attempts, state = obstructed
    backend, graph = _store(tmp_path)
    state["blocked"] = True
    graph.add_node("note", "big", text=BIG)
    for i in range(12):
        graph.add_node("note", f"n{i}", text=SMALL)
    assert len(attempts) == 1                       # tried once, then backed off
    assert len(_skips(backend)) == 1 and len(backend.warnings) == 1
    state["blocked"] = False
    fresh = JournalBackend(tmp_path / "p.memory").open()        # every write is there
    assert {node.id for node in fresh.nodes("note")} == {"big"} | {f"n{i}" for i in range(12)}


def test_writes_do_not_each_pay_the_replace_retry(tmp_path, monkeypatch):
    real_replace = os.replace

    def held_open(src, dst):                        # what a viewer holding the file causes
        if os.fspath(dst).endswith("p.memory"):
            raise PermissionError(13, "the snapshot is held open by another program")
        return real_replace(src, dst)

    backend, graph = _store(tmp_path, replace_retry_seconds=0.3)
    monkeypatch.setattr(os, "replace", held_open)
    started = time.perf_counter()
    graph.add_node("note", "big", text=BIG)
    for i in range(9):
        graph.add_node("note", f"n{i}", text=SMALL)
    elapsed = time.perf_counter() - started
    monkeypatch.undo()
    assert elapsed < 1.5, f"10 writes took {elapsed:.2f}s: each retried the replace"
    assert len(backend.warnings) == 1


def test_compaction_resumes_once_the_obstruction_is_gone(tmp_path, obstructed):
    attempts, state = obstructed
    backend, graph = _store(tmp_path)
    state["blocked"] = True
    graph.add_node("note", "a", text=BIG)
    assert len(attempts) == 1
    state["blocked"] = False
    generation = backend.generation
    assert backend.checkpoint() is True             # an explicit checkpoint always tries
    assert backend.generation != generation
    # Blocked once more: automatic compaction backs off; lifted, it resumes once the journal
    # has doubled since the skipped attempt.
    state["blocked"] = True
    graph.add_node("note", "b", text=BIG)
    assert len(attempts) == 2
    state["blocked"] = False
    generation, skipped_at = backend.generation, backend.offset
    graph.add_node("note", "c", text=SMALL)
    assert backend.generation == generation          # still backing off
    graph.add_node("note", "d", text=BIG)            # the journal has now doubled
    assert backend.generation != generation and backend.offset < skipped_at
    assert len(_skips(backend)) == 2                # one per run of skipped compactions


def test_the_back_off_also_ends_after_a_while(tmp_path, obstructed, monkeypatch):
    attempts, state = obstructed
    monkeypatch.setattr(backend_mod, "_COMPACT_BACKOFF_SECONDS", 0.0)
    backend, graph = _store(tmp_path)
    state["blocked"] = True
    graph.add_node("note", "big", text=BIG)
    for i in range(3):
        graph.add_node("note", f"n{i}", text=SMALL)
    assert len(attempts) == 4                       # no waiting time: tried on every write
    assert len(_skips(backend)) == 1
