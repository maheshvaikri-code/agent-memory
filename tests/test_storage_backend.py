import json
import os
import threading

import pytest
from ison_graph import DuplicateNodeError, ISONGraph

from agent_memory.storage import Backend, FileLock, JournalBackend, journal
from agent_memory.storage.errors import StoreCorrupt, StoreLockTimeout
from agent_memory.storage.shared_graph import META_ID, META_TYPE
from storage_helpers import run_py


def opened(path, **kw):
    backend = JournalBackend(path, **kw)
    return backend, backend.open()


def journal_lines(backend):
    with open(backend.journal_path, "rb") as handle:
        return handle.read().splitlines()


def test_open_creates_the_three_files_and_a_loadable_snapshot(tmp_path):
    backend, graph = opened(tmp_path / "p.memory")
    assert (tmp_path / "p.memory").exists() and (tmp_path / "p.memory.journal").exists()
    assert (tmp_path / "p.memory.lock").exists()
    assert ISONGraph.load(tmp_path / "p.memory").has_node(META_TYPE, META_ID)
    assert isinstance(backend, Backend)


def test_a_write_is_visible_to_a_second_open(tmp_path):
    path = tmp_path / "p.memory"
    _, g1 = opened(path)
    g1.add_node("note", "a", text="x")
    _, g2 = opened(path)
    assert g2.get_node("note", "a").properties["text"] == "x"


def test_refresh_if_stale_applies_only_what_is_new(tmp_path):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path)
    b2, g2 = opened(path)
    assert b2.refresh_if_stale() == 0
    g1.add_node("note", "a")
    g1.add_node("note", "b")
    assert b2.refresh_if_stale() == 2 and g2.has_node("note", "b")
    assert b2.refresh_if_stale() == 0
    g2.add_node("note", "c")
    assert b2.refresh_if_stale() == 0          # our own write is not "new"
    assert b1.refresh() == 1 and g1.has_node("note", "c")


def test_a_transaction_is_one_journal_line(tmp_path):
    backend, graph = opened(tmp_path / "p.memory")
    before = len(journal_lines(backend))
    with graph.transaction():
        graph.add_node("note", "a")
        graph.add_node("note", "b")
        graph.add_edge("LINKS", ("note", "a"), ("note", "b"))
    assert len(journal_lines(backend)) == before + 1
    assert len(json.loads(journal_lines(backend)[-1][9:])["ops"]) == 3


def test_a_failed_body_still_commits_what_it_applied(tmp_path):
    path = tmp_path / "p.memory"
    _, g1 = opened(path)
    b2, g2 = opened(path)
    with pytest.raises(RuntimeError):
        with g1.transaction():
            g1.add_node("note", "a")
            raise RuntimeError("boom")
    b2.refresh()
    assert g2.has_node("note", "a")


def test_serial_semantics_a_second_instance_sees_the_first_instances_write(tmp_path):
    path = tmp_path / "p.memory"
    _, g1 = opened(path)
    _, g2 = opened(path)
    g1.add_node("note", "a")
    with pytest.raises(DuplicateNodeError):
        g2.add_node("note", "a")


def test_unjournalable_value_leaves_the_store_untouched(tmp_path):
    import datetime
    backend, graph = opened(tmp_path / "p.memory")
    lines = journal_lines(backend)
    with pytest.raises(TypeError):
        graph.add_node("note", "a", when=datetime.datetime.now())
    assert not graph.has_node("note", "a") and journal_lines(backend) == lines


def test_checkpoint_folds_the_journal_and_other_instances_follow(tmp_path):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path)
    b2, g2 = opened(path)
    g1.add_node("note", "a")
    old_generation = b1.generation
    assert b1.checkpoint() is True
    assert b1.generation != old_generation and len(journal_lines(b1)) == 1   # header only
    assert b2.refresh_if_stale() >= 0 and g2.has_node("note", "a")
    assert b2.generation == b1.generation
    g2.add_node("note", "b")
    b1.refresh_if_stale()
    assert g1.has_node("note", "b")
    assert ISONGraph.load(path).has_node("note", "a")


def test_automatic_compaction_when_the_journal_outgrows_the_snapshot(tmp_path):
    backend, graph = opened(tmp_path / "p.memory", compact_min_bytes=0)
    first = backend.generation
    for i in range(120):
        graph.add_node("note", f"n{i}", text="x" * 40)
    assert backend.generation != first
    assert ISONGraph.load(tmp_path / "p.memory").has_node("note", "n0")


def test_torn_tail_is_truncated_by_the_next_writer(tmp_path):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path)
    g1.add_node("note", "a")
    with open(b1.journal_path, "ab") as handle:        # a crashed writer's half line
        handle.write(b'0000abcd {"seq":2,"actor":"x","ts":"t","ops":[["n","note","b",{}')
    b2, g2 = opened(path)
    assert g2.has_node("note", "a") and not g2.has_node("note", "b")
    g2.add_node("note", "c")
    tail = journal.read_tail(b2.journal_path, 0)
    assert not tail.torn and [t["seq"] for t in tail.txs] == [1, 2]
    b1.refresh()
    assert g1.has_node("note", "c")


def test_corrupt_middle_line_raises_store_corrupt(tmp_path):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path)
    for name in "abc":
        g1.add_node("note", name)
    data = bytearray(open(b1.journal_path, "rb").read())
    second = data.index(b"\n", data.index(b"\n") + 1) + 1       # start of the 2nd tx line
    data[second + 14] ^= 0x01
    open(b1.journal_path, "wb").write(bytes(data))
    with pytest.raises(StoreCorrupt):
        JournalBackend(path).open()


def test_a_stale_journal_from_a_crashed_compaction_is_ignored(tmp_path):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path)
    g1.add_node("note", "a")
    g1.add_node("note", "b")
    old = open(b1.journal_path, "rb").read()
    b1.checkpoint()
    open(b1.journal_path, "wb").write(old)         # crash before the journal was rewritten
    b2, g2 = opened(path)
    assert g2.has_node("note", "a") and g2.has_node("note", "b") and b2.warnings == []
    g2.add_node("note", "c")
    assert journal.read_tail(b2.journal_path, 0).generation == b2.generation
    assert JournalBackend.read_only(path).has_node("note", "c")


def test_a_legacy_file_without_meta_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.memory"
    legacy = ISONGraph(name="old")
    legacy.add_node("note", "a", text="kept")
    legacy.save(path)
    backend, graph = opened(path)
    assert graph.get_node("note", "a").properties["text"] == "kept"
    assert graph.has_node(META_TYPE, META_ID)
    assert ISONGraph.load(path).has_node("note", "a")


def test_close_checkpoints_and_then_refuses_writes(tmp_path):
    path = tmp_path / "p.memory"
    backend, graph = opened(path)
    graph.add_node("note", "a")
    backend.close()
    assert len(journal_lines(backend)) == 1
    with pytest.raises(ValueError):
        graph.add_node("note", "b")
    assert opened(path)[1].has_node("note", "a")


def test_path_forms(tmp_path, monkeypatch):
    weird = tmp_path / "dir with spaces" / "ünï"
    weird.mkdir(parents=True)
    _, graph = opened(weird / "p.memory")                    # pathlib.Path, spaces, non-ASCII
    graph.add_node("note", "a")
    assert opened(str(weird / "p.memory"))[1].has_node("note", "a")
    monkeypatch.chdir(tmp_path)
    _, rel = opened("rel.memory")                            # relative
    rel.add_node("note", "r")
    monkeypatch.chdir(weird)                                 # the backend must not care about cwd
    rel.add_node("note", "r2")
    assert opened(tmp_path / "rel.memory")[1].has_node("note", "r2")


def test_lock_timeout_is_reported(tmp_path):
    path = tmp_path / "p.memory"
    _, g1 = opened(path)
    started, release = threading.Event(), threading.Event()

    def hold():
        with g1.transaction():
            started.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(10)
    with pytest.raises(StoreLockTimeout):
        JournalBackend(path, lock_timeout=0.2).open()
    release.set()
    thread.join()


def test_a_failed_append_is_rolled_back_and_not_visible_to_others(tmp_path, monkeypatch):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path, durability="fsync")
    b2, g2 = opened(path)
    g1.add_node("note", "kept")
    real = os.fsync

    def disk_full(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", disk_full)
    with pytest.raises(OSError):
        g1.add_node("note", "lost")
    monkeypatch.setattr(os, "fsync", real)
    assert g1.has_node("note", "kept") and not g1.has_node("note", "lost")
    b2.refresh()
    assert g2.has_node("note", "kept") and not g2.has_node("note", "lost")
    g1.add_node("note", "after")                      # the store still works
    b2.refresh()
    assert g2.has_node("note", "after")


CHILD = """
import sys
from agent_memory.storage import JournalBackend
backend = JournalBackend(sys.argv[1], agent="child")
graph = backend.open()
graph.add_node("note", "from-child", text="hi")
"""


def test_another_process_writes_and_this_one_sees_it(tmp_path):
    path = tmp_path / "p.memory"
    backend, graph = opened(path)
    result = run_py(CHILD, path)
    assert result.returncode == 0, result.stderr
    assert backend.refresh_if_stale() == 1
    assert graph.get_node("note", "from-child").properties["text"] == "hi"


def test_read_only_loads_snapshot_plus_journal_without_a_backend(tmp_path):
    path = tmp_path / "p.memory"
    _, graph = opened(path)
    graph.add_node("note", "a")
    view = JournalBackend.read_only(path)
    assert view.has_node("note", "a") and view._backend is None
    view.add_node("note", "local-only")                      # inert: does not touch the store
    assert not opened(path)[1].has_node("note", "local-only")


# -- beyond the plan: rulings and defects found while implementing ---------------------------


def test_an_unreadable_journal_is_never_mistaken_for_an_absent_one(tmp_path, monkeypatch):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path)
    b2, g2 = opened(path)
    g1.add_node("note", "a")
    before = open(b1.journal_path, "rb").read()
    held_during_read = []

    def unreadable(_path, _offset):
        held_during_read.append(b2._lock.held)
        return journal.Tail(generation=None, error="PermissionError: [WinError 32] in use")

    monkeypatch.setattr(journal, "read_tail", unreadable)
    with pytest.raises(StoreCorrupt):
        b2.refresh_if_stale()
    assert held_during_read == [False, True]      # tried lockless, declared only under the lock
    with pytest.raises(StoreCorrupt):
        g2.add_node("note", "b")                  # a write must not reset the journal either
    with pytest.raises(StoreCorrupt):
        b2.checkpoint()
    monkeypatch.undo()
    assert open(b1.journal_path, "rb").read() == before
    assert not g2.has_node("note", "b")
    assert b2.refresh() == 1 and g2.has_node("note", "a")


def test_a_writer_behind_a_compaction_whose_journal_rewrite_crashed_follows_the_snapshot(tmp_path):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path)
    b2, g2 = opened(path)
    g1.add_node("note", "a")
    b1.checkpoint()
    open(b1.journal_path, "wb").close()         # crashed after truncating, before the header
    g2.add_node("note", "b")                    # b2 never saw "a" nor the new generation
    assert g2.has_node("note", "a")
    fresh = opened(path)[1]
    assert fresh.has_node("note", "a") and fresh.has_node("note", "b")


def test_a_reader_that_saw_a_rolled_back_append_rebuilds_from_disk(tmp_path, monkeypatch):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path, durability="fsync")
    b2, g2 = opened(path)
    real = os.fsync

    def fails_after_another_process_looked(fd):
        b2.refresh_if_stale()                   # the complete line is readable before rollback
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", fails_after_another_process_looked)
    with pytest.raises(OSError):
        g1.add_node("note", "ghost")
    monkeypatch.setattr(os, "fsync", real)
    assert g2.has_node("note", "ghost") and not g1.has_node("note", "ghost")
    g1.add_node("note", "real")
    b2.refresh_if_stale()
    assert g2.has_node("note", "real") and not g2.has_node("note", "ghost")
    assert b2.seq == b1.seq
    assert any("rebuilt" in warning for warning in b2.warnings)


def test_a_change_inside_one_mtime_tick_is_still_seen(tmp_path):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path)
    b2, g2 = opened(path)
    st = os.stat(b1.journal_path)
    g1.add_node("note", "a")
    b1.checkpoint()                             # header-only again: the same size as before
    os.utime(b1.journal_path, ns=(st.st_atime_ns, st.st_mtime_ns))   # and the same coarse mtime
    assert os.path.getsize(b1.journal_path) == st.st_size
    b2.refresh_if_stale()
    assert g2.has_node("note", "a")


def test_close_while_another_process_holds_the_lock_warns_and_keeps_the_data(tmp_path):
    path = tmp_path / "p.memory"
    backend, graph = opened(path, lock_timeout=0.2)
    graph.add_node("note", "a")
    with FileLock(backend.lock_path):           # a second lock instance contends through the OS
        backend.close()
    assert any("close" in warning for warning in backend.warnings)
    with pytest.raises(ValueError):
        graph.add_node("note", "b")
    assert opened(path)[1].has_node("note", "a")


def test_checkpoint_inside_a_transaction_is_refused(tmp_path):
    path = tmp_path / "p.memory"
    backend, graph = opened(path)
    with graph.transaction():
        graph.add_node("note", "a")
        with pytest.raises(RuntimeError):
            backend.checkpoint()
    assert len(journal_lines(backend)) == 2
    reopened = JournalBackend(path)
    assert reopened.open().has_node("note", "a") and reopened.warnings == []


def test_a_failed_automatic_compaction_does_not_fail_the_write(tmp_path, monkeypatch):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path, compact_min_bytes=0)
    b2, g2 = opened(path)

    def denied(generation):
        raise PermissionError(13, "the journal is held open")

    monkeypatch.setattr(b1, "_reset_journal", denied)
    g1.add_node("note", "a", text="x" * 400)    # the journal outgrows the snapshot
    monkeypatch.undo()
    assert any("compaction" in warning for warning in b1.warnings)
    b2.refresh()
    assert g2.has_node("note", "a")
    g1.add_node("note", "b")
    fresh = opened(path)[1]
    assert fresh.has_node("note", "a") and fresh.has_node("note", "b")


def test_another_thread_reading_waits_for_an_in_flight_transaction(tmp_path):
    path = tmp_path / "p.memory"
    b1, g1 = opened(path)
    _, other = opened(path)
    other.add_node("note", "a")
    inside, release = threading.Event(), threading.Event()

    def writer():
        with g1.transaction():                  # applies "a" while catching up
            g1.add_node("note", "b")
            inside.set()
            release.wait(10)

    reader = threading.Thread(target=b1.refresh_if_stale)
    thread = threading.Thread(target=writer)
    thread.start()
    assert inside.wait(10)
    reader.start()
    reader.join(0.3)
    blocked = reader.is_alive()                 # it must not touch the graph mid-transaction
    release.set()
    thread.join()
    reader.join(10)
    assert blocked
    assert g1.has_node("note", "a") and g1.has_node("note", "b") and b1.warnings == []
