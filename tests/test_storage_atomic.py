import os

import pytest
from ison_graph import ISONGraph

from agent_memory import AgentMemory
from agent_memory.storage.atomic import atomic_write_text
from agent_memory.storage.errors import (AgentMemoryError, StoreCorrupt, StoreInUse,
                                         StoreLockTimeout)


def test_creates_and_replaces_without_leaving_temp_files(tmp_path):
    target = tmp_path / "a.txt"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]


def test_retries_permission_error_then_succeeds(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_text("old")
    real, calls = os.replace, {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    atomic_write_text(target, "new", retry_seconds=2.0)
    assert target.read_text() == "new" and calls["n"] == 3


def test_gives_up_keeping_the_old_file_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_text("old")

    def denied(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(PermissionError):
        atomic_write_text(target, "new", retry_seconds=0.05)
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]


def test_failed_fsync_keeps_the_old_file(tmp_path, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_text("old")

    def boom(fd):
        raise OSError("disk")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]


def test_text_is_stored_with_lf_newlines_and_utf8(tmp_path):
    text = "a\nb\n— ünï\n"
    atomic_write_text(tmp_path / "u.txt", text)
    assert (tmp_path / "u.txt").read_bytes() == text.encode("utf-8")


def test_save_round_trips(tmp_path):
    memory = AgentMemory("t")
    memory.remember("convention", {"name": "n", "rule": "r"})
    path = tmp_path / "p.memory"
    memory.save(str(path))
    assert AgentMemory.load(str(path)).graph.to_ison() == memory.graph.to_ison()
    assert [p.name for p in tmp_path.iterdir()] == ["p.memory"]


def test_save_isonl_suffix_is_still_supported(tmp_path):
    memory = AgentMemory("t")
    memory.remember("convention", {"name": "n", "rule": "r"})
    path = tmp_path / "p.isonl"
    memory.save(str(path))
    assert ISONGraph.load(path).node_count() == memory.graph.node_count()


def test_error_hierarchy():
    assert issubclass(StoreLockTimeout, (AgentMemoryError, TimeoutError))
    assert issubclass(StoreCorrupt, AgentMemoryError)
    assert issubclass(StoreInUse, AgentMemoryError)
