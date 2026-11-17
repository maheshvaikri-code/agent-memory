# Concurrency-Safe Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let several agent processes on one machine share one memory file with no lost writes, no duplicates, no torn files and crash safety.

**Architecture:** The existing `.memory` file stays the snapshot. Every graph mutation is recorded by `SharedGraph` (an `ISONGraph` subclass) and appended, one transaction per line, to a checksummed journal under a cross-process file lock; `JournalBackend` owns open, refresh, transaction, compaction and recovery. `AgentMemory.open()` turns shared mode on; `load`/`save` keep working, and `save` becomes atomic.

**Tech Stack:** Python >= 3.9 standard library only (`os`, `msvcrt`/`fcntl`, `zlib`, `json`, `threading`), `ison-graph >= 1.4.1` public API, pytest.

**Spec:** `docs/superpowers/specs/2026-11-17-concurrency-safe-storage-design.md` (approved). Read it first; it is the binding authority.

## Global Constraints

- Python `>=3.9`; every new module starts with `from __future__ import annotations`; no `X | Y` runtime type syntax; no new third-party dependency.
- Use only the public `ISONGraph` API (`add_node`, `update_node`, `add_edge`, `remove_node`, `remove_edge`, `nodes`, `edges`, `has_node`, `has_edge`, `get_node`, `to_ison`, `from_ison`, `node_count`). Never touch `_nodes`, `_edges` and the like.
- New package `src/agent_memory/storage/`. Store files: `<path>` (snapshot), `<path>.journal`, `<path>.lock`.
- Journal format, exactly: line 1 header `{"journal":1,"generation":"<id>"}`; each later line `<8 lowercase hex crc32 of the body> <body>\n` where body is JSON `{"seq":int,"actor":str,"ts":str,"ops":[...]}`. Ops: `["n",type,id,props]`, `["u",type,id,props]`, `["e",rel,[st,sid],[tt,tid],props]`, `["rn",type,id]`, `["re",rel,[st,sid],[tt,tid]]`.
- Snapshot carries node type `store_meta`, id `journal`, props `generation` (str) and `seq` (int). It is excluded from `node_count()`.
- Defaults: lock timeout 10 s; snapshot-replace retry 2 s; compaction when journal > 1 MiB **and** larger than the snapshot; `durability="flush"` (`"fsync"` opt-in).
- Locking: one exclusive lock, no shared mode; the OS releases it when the holder dies.
- Lockless reads never raise `StoreCorrupt`: on any anomaly they retry under the lock, and only there is corruption declared.
- The existing 258-test suite must pass unchanged. Single-process behaviour is identical to today.
- Commit messages: Conventional-style subject, **no `Co-Authored-By` trailer and no other attribution line**. Commits are authored by the repo's configured git user.
- Test command: `python -m pytest tests -q` (Task 1 adds `tests/conftest.py`, after which no `PYTHONPATH` is needed; before that use `PYTHONPATH=src`).

### Ruling that refines spec §4.3

The spec wraps `MemoryWriter`, anchors, sessions, consolidation and "~4 store sites" in explicit transactions, and names "the audit of write sites can miss one" as its main risk. This plan removes that risk class for stores: `BaseStore.__init_subclass__` wraps **every public method of every store** in a transaction (Task 7). Cost if wrong: store reads also take the lock, so they serialise briefly against writers; reversible by deleting one hook. A test asserts every public store method is wrapped.

## Review Focus

The inputs a person is most likely to hit that no obvious test would exercise. Each line names the test that pins it.

1. Two processes record the **same fact at the same instant** — one record, not two. (Task 7 `test_two_instances_same_fact_merge_not_duplicate`; Task 8 stress.)
2. A process is killed **mid-append** or **mid-compaction** — the next open works and keeps every acknowledged write. (Task 5 torn tail / stale journal; Task 8 crash injection.)
3. A reader runs **while another process compacts** — it never errors or sees a partial store. (Task 8 `test_readers_survive_compaction`.)
4. A property value that cannot be journaled (a `datetime`) — fails cleanly **before** mutating anything. (Task 4.)
5. The store path is a `pathlib.Path`, relative, or lives in a directory with **spaces and non-ASCII** characters. (Task 5.)
6. A **legacy `.memory` file** with no meta node opens and is upgraded in place without losing data. (Task 5.)
7. The writer's per-process text index goes **stale** when another process adds a record — the merge rule must still find it. (Task 7 `test_remote_record_found_by_writer_index`.)

---

### Task 1: Errors, atomic file write, atomic `save()`, test import path

**Files:**
- Create: `src/agent_memory/storage/__init__.py`, `src/agent_memory/storage/errors.py`, `src/agent_memory/storage/atomic.py`, `tests/conftest.py`, `tests/test_storage_atomic.py`
- Modify: `src/agent_memory/memory.py` (`save`, imports)

**Interfaces:**
- Produces: `atomic_write_text(path, text, *, retry_seconds=2.0, fsync=True) -> None`; exceptions `AgentMemoryError`, `StoreLockTimeout(AgentMemoryError, TimeoutError)`, `StoreCorrupt(AgentMemoryError)`, `StoreInUse(AgentMemoryError)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_storage_atomic.py`

```python
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
```

- [ ] **Step 2: Run to verify failure** — `PYTHONPATH=src python -m pytest tests/test_storage_atomic.py -q` → FAIL (`No module named 'agent_memory.storage'`).

- [ ] **Step 3: Implement**

`src/agent_memory/storage/errors.py`:

```python
"""Errors raised by the shared-store layer."""
from __future__ import annotations


class AgentMemoryError(Exception):
    """Base class for agent-memory errors."""


class StoreLockTimeout(AgentMemoryError, TimeoutError):
    """The store's file lock could not be taken within the timeout."""


class StoreCorrupt(AgentMemoryError):
    """The snapshot or journal is damaged in a way recovery cannot repair."""


class StoreInUse(AgentMemoryError):
    """A plain (non-shared) memory tried to overwrite a store that has a journal."""
```

`src/agent_memory/storage/atomic.py`:

```python
"""Write a text file so readers never see a half-written one."""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Union


def atomic_write_text(path: Union[str, "os.PathLike[str]"], text: str, *,
                      retry_seconds: float = 2.0, fsync: bool = True) -> None:
    """Write `text` (UTF-8, LF newlines) to `path` via a temp file in the same directory.

    On Windows `os.replace` raises PermissionError while another process has the target open;
    it is retried with backoff for `retry_seconds` and then re-raised. The previous file is
    never touched until the replace succeeds, and the temp file never outlives the call.
    """
    target = Path(path)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        deadline = time.monotonic() + retry_seconds
        delay = 0.005
        while True:
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)
    finally:
        try:
            os.unlink(tmp)          # still there only when the replace did not happen
        except OSError:
            pass
```

`src/agent_memory/storage/__init__.py`:

```python
"""Shared-store layer: lock, journal, shared graph, backend. Built up task by task."""
from .atomic import atomic_write_text
from .errors import AgentMemoryError, StoreCorrupt, StoreInUse, StoreLockTimeout

__all__ = ["atomic_write_text", "AgentMemoryError", "StoreCorrupt", "StoreInUse",
           "StoreLockTimeout"]
```

`tests/conftest.py` (makes plain `pytest` use this checkout, not the sibling editable install, and lets tests import the helper modules that live beside them, e.g. `storage_helpers`):

```python
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for entry in (str(HERE), str(HERE.parent / "src")):      # src ends up first, tests/ second
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)
```

In `src/agent_memory/memory.py` add `from pathlib import Path` if missing and `from .storage.atomic import atomic_write_text`, and replace `save`:

```python
    def save(self, path: str) -> None:
        """Save memory to file. The write is atomic: a reader never sees half a file."""
        target = str(path)
        text = self.graph.to_isonl() if Path(target).suffix == ".isonl" else self.graph.to_ison()
        atomic_write_text(target, text)
```

- [ ] **Step 4: Run** — `python -m pytest tests -q` (no PYTHONPATH) → all green: the new 7 tests plus the existing 258 (265 passed, 1 skipped, 3 deselected).

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/storage src/agent_memory/memory.py tests/conftest.py tests/test_storage_atomic.py
git commit -m "feat: atomic save, storage package skeleton, store error types"
```

---

### Task 2: Cross-process file lock and fault hooks

**Files:**
- Create: `src/agent_memory/storage/lock.py`, `src/agent_memory/storage/faults.py`, `tests/storage_helpers.py`, `tests/test_storage_lock.py`
- Modify: `src/agent_memory/storage/__init__.py` (export `FileLock`)

**Interfaces:**
- Produces: `FileLock(path, timeout=10.0, poll=0.005)` with `acquire()`, `release()`, `held`, context-manager use, re-entrant per instance; `faults.fire(point) -> bool`, `faults.hit(point)`; test helpers `run_py(code, *args, env=None, timeout=60)` and `spawn_py(code, *args, env=None)` in `tests/storage_helpers.py`.

- [ ] **Step 1: Write the failing tests**

`tests/storage_helpers.py`:

```python
import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
TESTS = str(Path(__file__).resolve().parent)


def child_env(extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([SRC, TESTS, env.get("PYTHONPATH", "")])
    env.update(extra or {})
    return env


def run_py(code, *args, env=None, timeout=60):
    return subprocess.run([sys.executable, "-c", code, *map(str, args)], capture_output=True,
                          text=True, env=child_env(env), timeout=timeout)


def spawn_py(code, *args, env=None):
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=child_env(env))
```

`tests/test_storage_lock.py`:

```python
import threading
import time

import pytest

from agent_memory.storage.errors import StoreLockTimeout
from agent_memory.storage.lock import FileLock
from storage_helpers import run_py, spawn_py


def test_is_reentrant_within_one_instance(tmp_path):
    lock = FileLock(tmp_path / "x.lock")
    with lock:
        with lock:
            assert lock.held
    assert not lock.held


def test_second_instance_times_out_while_first_holds(tmp_path):
    first, second = FileLock(tmp_path / "x.lock"), FileLock(tmp_path / "x.lock", timeout=0.15)
    with first:
        with pytest.raises(StoreLockTimeout):
            second.acquire()
    with second:        # free again once the first lets go
        pass


def test_threads_of_one_instance_are_serialised(tmp_path):
    lock, inside, worst = FileLock(tmp_path / "x.lock"), [0], [0]

    def work():
        for _ in range(20):
            with lock:
                inside[0] += 1
                worst[0] = max(worst[0], inside[0])
                time.sleep(0.001)
                inside[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert worst[0] == 1


HOLDER = """
import sys, time
from agent_memory.storage.lock import FileLock
path, log, tag = sys.argv[1:4]
with FileLock(path, timeout=20):
    with open(log, "a") as f:
        f.write(f"{tag} start {time.monotonic():.6f}\\n")
    time.sleep(0.3)
    with open(log, "a") as f:
        f.write(f"{tag} end {time.monotonic():.6f}\\n")
"""


def test_two_processes_never_overlap(tmp_path):
    log = tmp_path / "log.txt"
    procs = [spawn_py(HOLDER, tmp_path / "x.lock", log, tag) for tag in "AB"]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
    rows = [line.split() for line in log.read_text().splitlines()]
    assert len(rows) == 4
    spans = {}
    for tag, kind, stamp in rows:
        spans.setdefault(tag, {})[kind] = float(stamp)
    first, second = sorted(spans.values(), key=lambda s: s["start"])
    assert first["end"] <= second["start"]       # monotonic clock is system-wide on both OSes


DIE_HOLDING = """
import os, sys
from agent_memory.storage.lock import FileLock
FileLock(sys.argv[1]).acquire()
os._exit(1)
"""


def test_a_dead_holder_releases_the_lock(tmp_path):
    run_py(DIE_HOLDING, tmp_path / "x.lock")
    with FileLock(tmp_path / "x.lock", timeout=3):
        pass


FAULT = """
from agent_memory.storage import faults
faults.hit("p"); print("first", flush=True)
faults.hit("p"); print("second", flush=True)
"""


def test_fault_hook_exits_on_the_nth_hit_only_when_armed(tmp_path):
    armed = run_py(FAULT, env={"AGENT_MEMORY_FAULT": "p", "AGENT_MEMORY_FAULT_AFTER": "2"})
    assert armed.returncode == 86 and "first" in armed.stdout and "second" not in armed.stdout
    other = run_py(FAULT, env={"AGENT_MEMORY_FAULT": "elsewhere"})
    assert other.returncode == 0 and "second" in other.stdout
    unarmed = run_py(FAULT)
    assert unarmed.returncode == 0
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_storage_lock.py -q` → FAIL (import error).

- [ ] **Step 3: Implement**

`src/agent_memory/storage/faults.py`:

```python
"""Crash-injection hooks for the test suite.

Set AGENT_MEMORY_FAULT=<point> (and optionally AGENT_MEMORY_FAULT_AFTER=<n>, default 1) and the
process exits abruptly with status 86 on the n-th time the named point is reached. With the
variable unset this is a couple of comparisons per call.
"""
from __future__ import annotations

import os

_POINT = os.environ.get("AGENT_MEMORY_FAULT", "")
_AFTER = int(os.environ.get("AGENT_MEMORY_FAULT_AFTER", "1") or 1)
_hits = 0


def fire(point: str) -> bool:
    """True exactly when `point` is the armed one and this is its n-th hit."""
    global _hits
    if not _POINT or point != _POINT:
        return False
    _hits += 1
    return _hits >= _AFTER


def hit(point: str) -> None:
    if fire(point):
        os._exit(86)
```

`src/agent_memory/storage/lock.py`:

```python
"""A cross-process exclusive file lock (stdlib only).

Windows has no shared lock mode in `msvcrt`, so there is one exclusive lock everywhere. The OS
drops it when the holder dies, so a crashed process never leaves a stale lock. Re-entrant per
instance; two instances (even in one process) contend through the OS.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional

from .errors import StoreLockTimeout

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, 0)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    def __init__(self, path, timeout: float = 10.0, poll: float = 0.005):
        self.path = os.fspath(path)
        self.timeout = timeout
        self.poll = poll
        self._thread_lock = threading.RLock()
        self._fd: Optional[int] = None
        self._depth = 0

    @property
    def held(self) -> bool:
        return self._depth > 0

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        if not self._thread_lock.acquire(timeout=self.timeout):
            raise StoreLockTimeout(f"could not take {self.path} within {self.timeout}s")
        try:
            if self._depth == 0:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
                try:
                    while not _try_lock(fd):
                        if time.monotonic() >= deadline:
                            raise StoreLockTimeout(
                                f"could not take {self.path} within {self.timeout}s")
                        time.sleep(self.poll)
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        if self._depth <= 0:
            raise RuntimeError("lock is not held")
        self._depth -= 1
        try:
            if self._depth == 0:
                fd, self._fd = self._fd, None
                try:
                    _unlock(fd)
                finally:
                    os.close(fd)
        finally:
            self._thread_lock.release()

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
```

Export `FileLock` from `storage/__init__.py`.

- [ ] **Step 4: Run** — `python -m pytest tests/test_storage_lock.py -q` → 6 passed; then the full suite → green.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/storage tests/storage_helpers.py tests/test_storage_lock.py
git commit -m "feat: cross-process file lock and crash-injection hooks"
```

---

### Task 3: Journal codec and tail reader

**Files:**
- Create: `src/agent_memory/storage/journal.py`, `tests/test_storage_journal.py`

**Interfaces:**
- Produces: `encode_header(generation) -> bytes`; `parse_header(line) -> Optional[str]`; `encode_op(op) -> str` (raises `TypeError` naming the op); `encode_tx(seq, actor, ts, op_texts) -> bytes`; `decode_tx(line) -> Optional[dict]`; `Tail` dataclass (`generation, txs, valid_end, size, torn, corrupt_at, shrunk, signature`); `read_tail(path, offset) -> Tail`.

- [ ] **Step 1: Write the failing tests** — `tests/test_storage_journal.py`

```python
import datetime

import pytest

from agent_memory.storage import journal


def tx(seq, name="a"):
    ops = [journal.encode_op(["n", "note", f"{name}{seq}", {"text": "hé"}])]
    return journal.encode_tx(seq, "agent-1", "2026-11-17T00:00:00+00:00", ops)


def make(path, n=3, generation="g1"):
    data = journal.encode_header(generation) + b"".join(tx(i) for i in range(1, n + 1))
    path.write_bytes(data)
    return data


def test_header_round_trip_and_rejects_junk():
    assert journal.parse_header(journal.encode_header("abc")) == "abc"
    for junk in (b"", b"nonsense\n", b'{"journal":2,"generation":"x"}\n',
                 b'{"journal":1}\n', b"\xff\xfe\n"):
        assert journal.parse_header(junk) is None


def test_tx_round_trip_and_checksum():
    line = tx(7)
    decoded = journal.decode_tx(line[:-1])
    assert decoded["seq"] == 7 and decoded["actor"] == "agent-1"
    assert decoded["ops"] == [["n", "note", "a7", {"text": "hé"}]]
    flipped = bytearray(line[:-1])
    flipped[-3] ^= 0x01
    assert journal.decode_tx(bytes(flipped)) is None
    assert journal.decode_tx(line[:20]) is None
    assert journal.decode_tx(b"") is None


def test_encode_op_rejects_unjournalable_values():
    with pytest.raises(TypeError) as exc:
        journal.encode_op(["n", "note", "a", {"when": datetime.datetime.now()}])
    assert "note" in str(exc.value)
    with pytest.raises(ValueError):
        journal.encode_op(["n", "note", "a", {"x": float("nan")}])


def test_one_transaction_is_one_line_even_with_newlines_in_values():
    op = journal.encode_op(["n", "note", "a", {"text": "line1\nline2"}])
    assert b"\n" not in journal.encode_tx(1, "a", "t", [op])[:-1]


def test_read_tail_whole_file(tmp_path):
    path = tmp_path / "j"
    data = make(path)
    tail = journal.read_tail(path, 0)
    assert tail.generation == "g1" and [t["seq"] for t in tail.txs] == [1, 2, 3]
    assert tail.valid_end == len(data) and not tail.torn and tail.corrupt_at is None
    assert tail.signature is not None


def test_read_tail_is_incremental(tmp_path):
    path = tmp_path / "j"
    make(path)
    first = journal.read_tail(path, 0)
    cut = len(journal.encode_header("g1")) + len(tx(1)) + len(tx(2))
    rest = journal.read_tail(path, cut)
    assert [t["seq"] for t in rest.txs] == [3] and rest.valid_end == first.valid_end


def test_torn_tail_is_ignored_and_reported(tmp_path):
    path = tmp_path / "j"
    data = make(path)
    with open(path, "ab") as handle:
        handle.write(tx(4)[:25])
    tail = journal.read_tail(path, 0)
    assert [t["seq"] for t in tail.txs] == [1, 2, 3]
    assert tail.torn and tail.valid_end == len(data) and tail.corrupt_at is None


def test_nul_filled_tail_counts_as_torn(tmp_path):
    path = tmp_path / "j"
    data = make(path)
    with open(path, "ab") as handle:
        handle.write(b"\x00" * 64)
    tail = journal.read_tail(path, 0)
    assert tail.torn and tail.valid_end == len(data) and len(tail.txs) == 3


def test_damaged_complete_line_is_corruption_not_a_torn_tail(tmp_path):
    path = tmp_path / "j"
    data = bytearray(make(path))
    start = len(journal.encode_header("g1")) + len(tx(1))
    data[start + 14] ^= 0x01                     # inside the 2nd line's JSON
    path.write_bytes(bytes(data))
    tail = journal.read_tail(path, 0)
    assert tail.corrupt_at == start and [t["seq"] for t in tail.txs] == [1]
    assert not tail.torn


def test_missing_file_unfinished_header_and_shrunk(tmp_path):
    assert journal.read_tail(tmp_path / "nope", 0).generation is None
    path = tmp_path / "j"
    path.write_bytes(b'{"journal":1,"gene')
    assert journal.read_tail(path, 0).generation is None
    data = make(path)
    assert journal.read_tail(path, len(data) + 500).shrunk
```

- [ ] **Step 2: Run to verify failure** — FAIL (`cannot import name 'journal'`).

- [ ] **Step 3: Implement** — `src/agent_memory/storage/journal.py`

```python
"""The journal file: header, checksummed transaction lines, and an incremental tail reader."""
from __future__ import annotations

import json
import os
import zlib
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

_SEPARATORS = (",", ":")


def encode_header(generation: str) -> bytes:
    return (json.dumps({"journal": 1, "generation": generation}, separators=_SEPARATORS)
            + "\n").encode("utf-8")


def parse_header(line: bytes) -> Optional[str]:
    try:
        data = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if (isinstance(data, dict) and data.get("journal") == 1
            and isinstance(data.get("generation"), str)):
        return data["generation"]
    return None


def encode_op(op: list) -> str:
    """JSON text of one op. Raises TypeError/ValueError, naming the op, for a value that
    cannot be journaled - called *before* the graph is touched."""
    try:
        return json.dumps(op, separators=_SEPARATORS, allow_nan=False)
    except TypeError as exc:
        raise TypeError(f"cannot journal {op[0]!r} on {op[1:3]!r}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"cannot journal {op[0]!r} on {op[1:3]!r}: {exc}") from exc


def encode_tx(seq: int, actor: str, ts: str, op_texts: List[str]) -> bytes:
    head = json.dumps({"seq": seq, "actor": actor, "ts": ts}, separators=_SEPARATORS)
    body = (head[:-1] + ',"ops":[' + ",".join(op_texts) + "]}").encode("utf-8")
    return b"%08x " % (zlib.crc32(body) & 0xFFFFFFFF) + body + b"\n"


def decode_tx(line: bytes) -> Optional[dict]:
    """The transaction in one line (without its newline), or None when it is not valid."""
    if len(line) < 10 or line[8:9] != b" ":
        return None
    try:
        crc = int(line[:8], 16)
    except ValueError:
        return None
    body = line[9:]
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        return None
    try:
        tx = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not (isinstance(tx, dict) and isinstance(tx.get("seq"), int)
            and isinstance(tx.get("ops"), list)):
        return None
    return tx


@dataclass
class Tail:
    generation: Optional[str]          # header generation; None when absent or unreadable
    txs: List[dict] = field(default_factory=list)
    valid_end: int = 0                 # byte offset just after the last valid line
    size: int = 0                      # file size seen
    torn: bool = False                 # an unfinished last line follows valid_end
    corrupt_at: Optional[int] = None   # byte offset of a damaged *complete* line
    shrunk: bool = False               # the file is shorter than the offset asked for
    signature: Optional[Tuple[int, int]] = None   # (size, mtime_ns) taken before reading


def read_tail(path, offset: int) -> Tail:
    """Read the transactions after byte `offset` (0 = from the start). Takes no lock."""
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return Tail(generation=None)
    with handle:
        st = os.fstat(handle.fileno())
        signature = (st.st_size, st.st_mtime_ns)
        first = handle.readline()
        generation = parse_header(first) if first.endswith(b"\n") else None
        if generation is None:
            return Tail(None, size=st.st_size, signature=signature)
        start = max(offset, len(first))
        handle.seek(start)
        rest = handle.read()
        actual = os.fstat(handle.fileno()).st_size
    if offset > actual:
        return Tail(generation, valid_end=len(first), size=actual, shrunk=True,
                    signature=signature)
    lines = rest.split(b"\n")
    fragment = lines.pop()               # b"" when the data ends with a newline
    txs: List[dict] = []
    pos, corrupt_at = start, None
    for raw in lines:
        tx = decode_tx(raw)
        if tx is None:
            corrupt_at = pos
            break
        txs.append(tx)
        pos += len(raw) + 1
    return Tail(generation, txs, pos, actual, torn=corrupt_at is None and bool(fragment),
                corrupt_at=corrupt_at, signature=signature)
```

- [ ] **Step 4: Run** — `python -m pytest tests/test_storage_journal.py -q` → 10 passed; full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/storage/journal.py tests/test_storage_journal.py
git commit -m "feat: journal codec with checksums and an incremental tail reader"
```

---

### Task 4: `SharedGraph` — recording and replay

**Files:**
- Create: `src/agent_memory/storage/shared_graph.py`, `tests/test_storage_shared_graph.py`
- Modify: `src/agent_memory/storage/__init__.py` (export `SharedGraph`, `META_TYPE`, `META_ID`)

**Interfaces:**
- Consumes: `journal.encode_op` (Task 3).
- Produces: `SharedGraph(ISONGraph)` with `_backend` (None = behaves as a plain `ISONGraph`), `transaction()`, `apply_op(op, warnings=None)`, `_replace_contents(other)`, `remote_ops` (int, bumped on every replayed op and every content replacement), `node_count()` excluding the meta node; constants `META_TYPE="store_meta"`, `META_ID="journal"`. A backend must provide `transaction()` and `record(text)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_storage_shared_graph.py`

```python
import contextlib
import datetime

import pytest
from ison_graph import DuplicateNodeError, ISONGraph

from agent_memory.storage import journal
from agent_memory.storage.shared_graph import META_ID, META_TYPE, SharedGraph


class FakeBackend:
    def __init__(self):
        self.ops, self.depth = [], 0

    @contextlib.contextmanager
    def transaction(self):
        self.depth += 1
        try:
            yield self
        finally:
            self.depth -= 1

    def record(self, text):
        assert self.depth > 0, "an op was recorded outside a transaction"
        self.ops.append(text)


def shared():
    graph = SharedGraph(name="t")
    graph._backend = FakeBackend()
    return graph


def test_inert_without_a_backend():
    graph = SharedGraph(name="t")
    graph.add_node("note", "a", text="x")
    graph.add_node("note", "b")
    graph.add_edge("LINKS", ("note", "a"), ("note", "b"))
    assert graph.has_edge("LINKS", ("note", "a"), ("note", "b"))
    with graph.transaction():
        graph.update_node("note", "a", text="y")


def test_from_ison_builds_a_shared_graph_without_recording():
    text = ISONGraph(name="x")
    text.add_node("note", "a", text="x")
    loaded = SharedGraph.from_ison(text.to_ison(), "x")
    assert isinstance(loaded, SharedGraph) and loaded._backend is None
    assert loaded.has_node("note", "a")


def test_each_mutator_records_exactly_one_op():
    graph = shared()
    backend = graph._backend
    graph.add_node("note", "a", text="x")
    graph.add_node("note", 7, text="y")
    graph.add_edge("LINKS", ("note", "a"), ("note", 7), weight=2)
    graph.update_node("note", "a", text="z")
    graph.remove_edge("LINKS", ("note", "a"), ("note", 7))
    graph.remove_node("note", 7)
    import json
    assert [json.loads(o)[0] for o in backend.ops] == ["n", "n", "e", "u", "re", "rn"]
    assert json.loads(backend.ops[1])[2] == 7           # int ids stay ints


def test_a_failing_mutation_records_nothing():
    graph = shared()
    graph.add_node("note", "a")
    before = len(graph._backend.ops)
    with pytest.raises(DuplicateNodeError):
        graph.add_node("note", "a")
    assert len(graph._backend.ops) == before


def test_unjournalable_value_fails_before_touching_the_graph():
    graph = shared()
    with pytest.raises(TypeError):
        graph.add_node("note", "a", when=datetime.datetime.now())
    assert not graph.has_node("note", "a") and graph._backend.ops == []


def test_replaying_recorded_ops_rebuilds_an_identical_graph():
    import json
    source = shared()
    source.add_node("note", "a", text="héllo", n=3, ok=True, none=None)
    source.add_node("note", 7, text="seven")
    source.add_edge("LINKS", ("note", "a"), ("note", 7), weight=2)
    source.update_node("note", "a", text="changed")
    source.add_node("note", "gone")
    source.remove_node("note", "gone")
    replica = SharedGraph(name="t")
    for text in source._backend.ops:
        replica.apply_op(json.loads(text))
    assert replica.to_ison() == source.to_ison()


def test_replaying_an_applied_op_is_idempotent_and_warns():
    graph = SharedGraph(name="t")
    warnings = []
    graph.apply_op(["n", "note", "a", {"text": "x"}], warnings)
    graph.apply_op(["n", "note", "a", {"text": "y"}], warnings)
    graph.apply_op(["rn", "note", "missing"], warnings)
    assert graph.get_node("note", "a").properties["text"] == "y" and len(warnings) == 2


def test_unknown_op_is_an_error():
    with pytest.raises(ValueError):
        SharedGraph(name="t").apply_op(["zz", "x"])


def test_remote_ops_counter_moves_on_replay_only():
    graph = shared()
    graph.add_node("note", "a")
    assert graph.remote_ops == 0
    graph.apply_op(["n", "note", "b", {}])
    assert graph.remote_ops == 1


def test_node_count_hides_the_meta_node():
    graph = SharedGraph(name="t")
    graph.add_node("note", "a")
    ISONGraph.add_node(graph, META_TYPE, META_ID, generation="g", seq=0)
    assert graph.node_count() == 1 and graph.has_node(META_TYPE, META_ID)


def test_replace_contents_swaps_everything():
    graph = SharedGraph(name="t")
    graph.add_node("note", "old")
    other = SharedGraph(name="t")
    other.add_node("note", "a", text="x")
    other.add_node("note", "b")
    other.add_edge("LINKS", ("note", "a"), ("note", "b"))
    graph._replace_contents(other)
    assert graph.to_ison() == other.to_ison() and graph.remote_ops >= 1
```

- [ ] **Step 2: Run to verify failure** — FAIL (no `shared_graph`).

- [ ] **Step 3: Implement** — `src/agent_memory/storage/shared_graph.py`

```python
"""An ISONGraph that records its mutations for the journal.

With no backend attached (`_backend is None`) it is a plain ISONGraph, which is also what it is
while a snapshot loads: `ISONGraph._build` calls `add_node`/`add_edge` on the instance.
"""
from __future__ import annotations

import contextlib
from typing import Any, Callable, List, Optional

from ison_graph import ISONGraph

from . import journal

META_TYPE = "store_meta"
META_ID = "journal"


class SharedGraph(ISONGraph):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._backend = None
        self.remote_ops = 0        # bumped whenever content arrives from outside this process

    # -- transactions ------------------------------------------------------

    def transaction(self):
        if self._backend is None:
            return contextlib.nullcontext()
        return self._backend.transaction()

    def node_count(self) -> int:
        count = super().node_count()
        return count - 1 if self.has_node(META_TYPE, META_ID) else count

    def _journaled(self, op: list, apply: Callable[[], Any]) -> Any:
        backend = self._backend
        with backend.transaction():
            text = journal.encode_op(op)        # fail before the graph is touched
            result = apply()
            backend.record(text)
            return result

    # -- mutators ----------------------------------------------------------

    def add_node(self, node_type, node_id, **properties):
        if self._backend is None:
            return ISONGraph.add_node(self, node_type, node_id, **properties)
        return self._journaled(["n", node_type, node_id, properties],
                               lambda: ISONGraph.add_node(self, node_type, node_id, **properties))

    def update_node(self, node_type, node_id, **properties):
        if self._backend is None:
            return ISONGraph.update_node(self, node_type, node_id, **properties)
        return self._journaled(["u", node_type, node_id, properties],
                               lambda: ISONGraph.update_node(self, node_type, node_id, **properties))

    def add_edge(self, rel_type, source, target, **properties):
        if self._backend is None:
            return ISONGraph.add_edge(self, rel_type, source, target, **properties)
        return self._journaled(["e", rel_type, list(source), list(target), properties],
                               lambda: ISONGraph.add_edge(self, rel_type, source, target, **properties))

    def remove_node(self, node_type, node_id):
        if self._backend is None:
            return ISONGraph.remove_node(self, node_type, node_id)
        return self._journaled(["rn", node_type, node_id],
                               lambda: ISONGraph.remove_node(self, node_type, node_id))

    def remove_edge(self, rel_type, source, target):
        if self._backend is None:
            return ISONGraph.remove_edge(self, rel_type, source, target)
        return self._journaled(["re", rel_type, list(source), list(target)],
                               lambda: ISONGraph.remove_edge(self, rel_type, source, target))

    # -- replay ------------------------------------------------------------

    def apply_op(self, op: list, warnings: Optional[List[str]] = None) -> None:
        """Apply one journaled op without recording it. Ops that are already in effect are
        applied idempotently and noted in `warnings`; they cannot arise from the protocol and
        indicate someone edited the files by hand."""
        note = warnings.append if warnings is not None else (lambda _message: None)
        code = op[0]
        self.remote_ops += 1
        if code == "n":
            _, node_type, node_id, props = op
            if self.has_node(node_type, node_id):
                note(f"journal replay: {node_type}:{node_id} already existed; updated in place")
                ISONGraph.update_node(self, node_type, node_id, **props)
            else:
                ISONGraph.add_node(self, node_type, node_id, **props)
        elif code == "u":
            _, node_type, node_id, props = op
            ISONGraph.update_node(self, node_type, node_id, **props)
        elif code == "e":
            _, rel, source, target, props = op
            source, target = tuple(source), tuple(target)
            if self.has_edge(rel, source, target):
                note(f"journal replay: edge {rel} {source}->{target} already existed")
            else:
                ISONGraph.add_edge(self, rel, source, target, **props)
        elif code == "rn":
            _, node_type, node_id = op
            if self.has_node(node_type, node_id):
                ISONGraph.remove_node(self, node_type, node_id)
            else:
                note(f"journal replay: {node_type}:{node_id} was already gone")
        elif code == "re":
            _, rel, source, target = op
            source, target = tuple(source), tuple(target)
            if self.has_edge(rel, source, target):
                ISONGraph.remove_edge(self, rel, source, target)
            else:
                note(f"journal replay: edge {rel} {source}->{target} was already gone")
        else:
            raise ValueError(f"unknown journal op {code!r}")

    def _replace_contents(self, other: ISONGraph) -> None:
        """Make this graph hold exactly what `other` holds, in place (stores, the writer and
        the retriever all keep a reference to this object)."""
        for edge in list(self.edges()):
            ISONGraph.remove_edge(self, edge.rel_type, edge.source, edge.target)
        for node in list(self.nodes()):
            ISONGraph.remove_node(self, node.type, node.id)
        for node in other.nodes():
            ISONGraph.add_node(self, node.type, node.id, **dict(node.properties))
        for edge in other.edges():
            ISONGraph.add_edge(self, edge.rel_type, edge.source, edge.target,
                               **dict(edge.properties))
        self.remote_ops += 1
```

Export from `storage/__init__.py`: `SharedGraph`, `META_TYPE`, `META_ID`.

- [ ] **Step 4: Run** — `python -m pytest tests/test_storage_shared_graph.py -q` → 10 passed; full suite green. If `test_each_mutator_records_exactly_one_op` shows a mutator recording twice (an `ISONGraph` method calling another overridden one), stop and report it; the fix is to call the private implementation, not to dedupe ops.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/storage tests/test_storage_shared_graph.py
git commit -m "feat: SharedGraph records mutations and replays journaled ops"
```

---

### Task 5: `JournalBackend` — open, refresh, transaction, compaction, recovery

**Files:**
- Create: `src/agent_memory/storage/backend.py`, `tests/test_storage_backend.py`
- Modify: `src/agent_memory/storage/__init__.py` (export `JournalBackend`, `Backend`)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `JournalBackend(path, *, agent="unknown", durability="flush", lock_timeout=10.0, compact_min_bytes=1 << 20, replace_retry_seconds=2.0)` with `open() -> SharedGraph`, `refresh() -> int`, `refresh_if_stale() -> int`, `transaction()` (re-entrant context manager), `record(text)`, `checkpoint() -> bool`, `close()`, attributes `path, journal_path, lock_path, graph, generation, seq, warnings`; static `JournalBackend.read_only(path, lock_timeout=10.0) -> SharedGraph` (snapshot + journal replayed, no backend attached); `Backend` runtime-checkable Protocol (`transaction`, `refresh`, `checkpoint`, `close`).

- [ ] **Step 1: Write the failing tests** — `tests/test_storage_backend.py`

```python
import json
import os
import threading

import pytest
from ison_graph import DuplicateNodeError, ISONGraph

from agent_memory.storage import Backend, JournalBackend, journal
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
```

- [ ] **Step 2: Run to verify failure** — FAIL (no `backend`).

- [ ] **Step 3: Implement** — `src/agent_memory/storage/backend.py`

```python
"""JournalBackend: the snapshot + journal store behind a shared AgentMemory.

Every mutation happens inside `transaction()`: take the exclusive lock, apply whatever other
processes appended since we last looked, run the body, append one checksummed line, release.
So the journal is a serial history and absolute values are safe. Reads call
`refresh_if_stale()`, which is one stat and takes no lock.
"""
from __future__ import annotations

import contextlib
import os
import time
import uuid
from pathlib import Path
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from ison_graph import ISONGraph

from ..record import utc_now
from . import faults, journal
from .atomic import atomic_write_text
from .errors import StoreCorrupt
from .lock import FileLock
from .shared_graph import META_ID, META_TYPE, SharedGraph

_TEMP_MAX_AGE = 60.0       # seconds before a stray temp snapshot is considered abandoned


@runtime_checkable
class Backend(Protocol):
    """The seam a future backend (SQLite, a server) implements."""

    def transaction(self): ...
    def refresh(self) -> int: ...
    def checkpoint(self) -> bool: ...
    def close(self) -> None: ...


def _stem(path: str) -> str:
    return Path(path).stem


def _generation() -> str:
    return uuid.uuid4().hex


def _signature(path: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_size, st.st_mtime_ns)


class JournalBackend:
    def __init__(self, path, *, agent: str = "unknown", durability: str = "flush",
                 lock_timeout: float = 10.0, compact_min_bytes: int = 1 << 20,
                 replace_retry_seconds: float = 2.0):
        if durability not in ("flush", "fsync"):
            raise ValueError(f"durability must be 'flush' or 'fsync', got {durability!r}")
        self.path = os.path.abspath(os.fspath(path))
        self.journal_path = self.path + ".journal"
        self.lock_path = self.path + ".lock"
        self.agent = agent
        self.durability = durability
        self.compact_min_bytes = compact_min_bytes
        self.replace_retry_seconds = replace_retry_seconds
        self._lock = FileLock(self.lock_path, timeout=lock_timeout)
        self.graph: Optional[SharedGraph] = None
        self.warnings: List[str] = []
        self.generation = ""
        self.seq = 0
        self.offset = 0
        self._signature: Optional[Tuple[int, int]] = None
        self._needs_reset = False
        self._dirty = False
        self._depth = 0
        self._pending: List[str] = []
        self._closed = False

    # -- opening -----------------------------------------------------------

    def open(self) -> SharedGraph:
        with self._lock:
            self._remove_stale_temp_files()
            if os.path.exists(self.path):
                graph = self._read_snapshot()
                if self._meta(graph) is None:                  # a legacy file
                    ISONGraph.add_node(graph, META_TYPE, META_ID, generation=_generation(), seq=0)
                    self._write_snapshot(graph)
                    self._needs_reset = True
            else:
                graph = SharedGraph(name=_stem(self.path))
                ISONGraph.add_node(graph, META_TYPE, META_ID, generation=_generation(), seq=0)
                self._write_snapshot(graph)
                self._needs_reset = True
            self._adopt(graph)
            self._catch_up()
            self._prepare_append()
        graph._backend = self
        return graph

    @staticmethod
    def read_only(path, lock_timeout: float = 10.0) -> SharedGraph:
        """Snapshot plus the journal replayed, as a plain graph with no backend attached."""
        backend = JournalBackend(path, lock_timeout=lock_timeout)
        if not os.path.exists(backend.path):
            raise FileNotFoundError(backend.path)
        with backend._lock:
            graph = backend._read_snapshot()
            if backend._meta(graph) is not None:
                backend._adopt(graph)
                backend._catch_up()
        return graph

    # -- snapshot helpers --------------------------------------------------

    def _read_snapshot(self) -> SharedGraph:
        try:
            text = Path(self.path).read_text(encoding="utf-8")
            return SharedGraph.from_ison(text, _stem(self.path))
        except Exception as exc:
            raise StoreCorrupt(f"snapshot {self.path} cannot be read: {exc}") from exc

    @staticmethod
    def _meta(graph: SharedGraph) -> Optional[Tuple[str, int]]:
        if not graph.has_node(META_TYPE, META_ID):
            return None
        props = graph.get_node(META_TYPE, META_ID).properties
        return str(props.get("generation", "")), int(props.get("seq", 0))

    def _write_snapshot(self, graph: SharedGraph) -> None:
        atomic_write_text(self.path, graph.to_ison(), retry_seconds=self.replace_retry_seconds)

    def _adopt(self, graph: SharedGraph) -> None:
        self.graph = graph
        self.generation, self.seq = self._meta(graph)
        self.offset = 0

    def _remove_stale_temp_files(self) -> None:
        base = Path(self.path)
        for tmp in base.parent.glob(f".{base.name}.*.tmp"):
            try:
                if time.time() - tmp.stat().st_mtime > _TEMP_MAX_AGE:
                    tmp.unlink()
            except OSError:
                pass

    # -- reading the journal -----------------------------------------------

    def _catch_up(self) -> int:
        """Apply what other processes appended. Call with the lock held."""
        return self._apply_tail(journal.read_tail(self.journal_path, self.offset), True)

    def _apply_tail(self, tail: journal.Tail, authoritative: bool) -> Optional[int]:
        """Apply `tail`. Returns the number of transactions applied, or None when a lockless
        read hit something unexpected and the caller must retry under the lock."""
        if tail.generation is None:                  # no journal, or its header is unfinished
            self._needs_reset = True
            return 0
        if tail.generation != self.generation:
            if not authoritative:
                return None
            fresh = self._read_snapshot()
            fresh_generation, fresh_seq = self._meta(fresh)
            if fresh_generation != self.generation:  # another process compacted
                self.graph._replace_contents(fresh)
                self.generation, self.seq, self.offset = fresh_generation, fresh_seq, 0
                tail = journal.read_tail(self.journal_path, 0)
            if tail.generation != self.generation:   # stale: already folded into the snapshot
                self._needs_reset = True
                self._signature = tail.signature
                return 0
        if tail.shrunk or tail.corrupt_at is not None:
            if not authoritative:
                return None
            where = tail.corrupt_at if tail.corrupt_at is not None else self.offset
            raise StoreCorrupt(f"journal {self.journal_path} is damaged at byte {where}")
        applied = 0
        for tx in tail.txs:
            if tx["seq"] <= self.seq:
                continue
            if tx["seq"] != self.seq + 1:
                if not authoritative:
                    return None
                raise StoreCorrupt(f"journal sequence gap: expected {self.seq + 1}, "
                                   f"found {tx['seq']}")
            try:
                for op in tx["ops"]:
                    self.graph.apply_op(op, self.warnings)
            except Exception as exc:
                if not authoritative:
                    return None
                raise StoreCorrupt(f"journal transaction {tx['seq']} cannot be applied: "
                                   f"{exc}") from exc
            self.seq = tx["seq"]
            applied += 1
        self.offset = tail.valid_end
        self._needs_reset = False
        self._signature = tail.signature
        return applied

    def refresh(self) -> int:
        with self._lock:
            return self._catch_up()

    def refresh_if_stale(self) -> int:
        """Apply new journal content if there is any. One stat when there is none; no lock."""
        if self._depth > 0 or self._closed:
            return 0
        if _signature(self.journal_path) == self._signature:
            return 0
        applied = self._apply_tail(journal.read_tail(self.journal_path, self.offset), False)
        return self.refresh() if applied is None else applied

    # -- writing -----------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self):
        if self._depth > 0:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        if self._closed:
            raise ValueError("this store is closed")
        self._lock.acquire()
        try:
            self._depth = 1
            self._pending = []
            self._catch_up()
            self._prepare_append()
            try:
                yield self
            finally:
                ops, self._pending = self._pending, []
                if ops:
                    self._commit(ops)
        finally:
            self._depth = 0
            self._lock.release()

    def record(self, text: str) -> None:
        if self._depth == 0:
            raise RuntimeError("an op was recorded outside a transaction")
        self._pending.append(text)

    def _prepare_append(self) -> None:
        """Make the journal end exactly at `offset`: create it, reset a stale one, or cut a
        torn tail left by a crashed writer."""
        if self._needs_reset or not os.path.exists(self.journal_path):
            self._reset_journal(self.generation)
        elif os.path.getsize(self.journal_path) > self.offset:
            with open(self.journal_path, "r+b") as handle:
                handle.truncate(self.offset)
                handle.flush()
                os.fsync(handle.fileno())
            self._signature = _signature(self.journal_path)

    def _reset_journal(self, generation: str) -> None:
        header = journal.encode_header(generation)
        with open(self.journal_path, "wb") as handle:
            handle.write(header)
            handle.flush()
            os.fsync(handle.fileno())
        self.offset = len(header)
        self._needs_reset = False
        self._signature = _signature(self.journal_path)

    def _commit(self, ops: List[str]) -> None:
        seq = self.seq + 1
        line = journal.encode_tx(seq, self.agent, utc_now(), ops)
        faults.hit("before_append")
        try:
            with open(self.journal_path, "ab") as handle:
                if faults.fire("mid_append"):
                    handle.write(line[: len(line) // 2])
                    handle.flush()
                    os._exit(86)
                handle.write(line)
                handle.flush()
                if self.durability == "fsync":
                    os.fsync(handle.fileno())
        except BaseException:
            self._resync()
            raise
        faults.hit("after_append")
        self.seq = seq
        self.offset += len(line)
        self._signature = _signature(self.journal_path)
        self._dirty = True
        if self.offset > self.compact_min_bytes and self.offset > os.path.getsize(self.path):
            self._compact()

    def _resync(self) -> None:
        """A commit failed: drop whatever reached the journal and rebuild memory from disk, so
        this process never holds a change that others cannot see."""
        try:
            with open(self.journal_path, "r+b") as handle:
                handle.truncate(self.offset)
            fresh = self._read_snapshot()
            self.graph._replace_contents(fresh)
            self.generation, self.seq = self._meta(fresh)
            self.offset = 0
            self._catch_up()
        except Exception as exc:           # never mask the original error
            self.warnings.append(f"resync after a failed append did not complete: {exc}")

    # -- compaction --------------------------------------------------------

    def checkpoint(self) -> bool:
        """Fold the journal into a new snapshot. False when it had to be skipped."""
        with self._lock:
            self._catch_up()
            self._prepare_append()
            return self._compact()

    def _compact(self) -> bool:
        graph = self.graph
        new_generation = _generation()
        old_generation, old_seq = self.generation, self.seq
        ISONGraph.update_node(graph, META_TYPE, META_ID, generation=new_generation, seq=self.seq)
        try:
            faults.hit("before_snapshot_replace")
            self._write_snapshot(graph)
        except OSError as exc:        # a third party holds the snapshot open (antivirus, editor)
            ISONGraph.update_node(graph, META_TYPE, META_ID, generation=old_generation,
                                  seq=old_seq)
            self.warnings.append(f"compaction skipped: {exc}")
            return False
        faults.hit("after_snapshot_replace")
        self.generation = new_generation
        self._reset_journal(new_generation)
        self._dirty = False
        return True

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._dirty:
                self.checkpoint()
        finally:
            self._closed = True
```

Export `JournalBackend` and `Backend` from `storage/__init__.py`.

- [ ] **Step 4: Run** — `python -m pytest tests/test_storage_backend.py -q` → 16 passed; full suite green. Then run the file 5 times in a row to catch timing flakiness (`for i in 1 2 3 4 5; do python -m pytest tests/test_storage_backend.py -q; done`).

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/storage tests/test_storage_backend.py
git commit -m "feat: JournalBackend with locked transactions, compaction and crash recovery"
```

---

### Task 6: `AgentMemory.open`, `refresh`, `checkpoint`, `close`, `load`/`save` semantics

**Files:**
- Modify: `src/agent_memory/memory.py`, `src/agent_memory/__init__.py`
- Create: `tests/test_shared_memory.py`

**Interfaces:**
- Consumes: `JournalBackend`, `SharedGraph` (Tasks 4–5).
- Produces: `AgentMemory.open(path, repo_root=None, agent="unknown", **backend_options)`, `AgentMemory.refresh() -> int`, `checkpoint() -> bool`, `close()`, `is_shared` property, context-manager use, `_sync()`; `load()` replays a journal; `save()` semantics per spec §5; `StoreInUse`/`StoreLockTimeout`/`StoreCorrupt`/`AgentMemoryError` exported from `agent_memory`.

- [ ] **Step 1: Write the failing tests** — `tests/test_shared_memory.py`

```python
import os

import pytest

from agent_memory import AgentMemory, StoreInUse


def test_open_creates_a_store_and_a_second_open_recalls_without_being_told(tmp_path):
    path = str(tmp_path / "p.memory")
    a = AgentMemory.open(path, agent="a")
    b = AgentMemory.open(path, agent="b")
    a.remember("convention", {"name": "naming", "rule": "cache keys use colon namespaces"})
    found = b.recall(task="how should I name cache keys")            # no refresh() call
    assert any("colon namespaces" in item.text for item in found.items)


def test_agent_self_node_exists_once_however_many_opens(tmp_path):
    path = str(tmp_path / "p.memory")
    for _ in range(3):
        AgentMemory.open(path)
    assert len(list(AgentMemory.open(path).graph.nodes("agent"))) == 1


def test_load_replays_the_journal(tmp_path):
    path = str(tmp_path / "p.memory")
    a = AgentMemory.open(path)
    a.remember("convention", {"name": "n", "rule": "unflushed rule"})
    loaded = AgentMemory.load(path)                           # nothing was checkpointed
    assert any("unflushed rule" in r.text for r in
               loaded.recall(task="unflushed rule").items)
    assert loaded.is_shared is False


def test_stats_do_not_count_the_meta_node(tmp_path):
    shared = AgentMemory.open(str(tmp_path / "p.memory"))
    plain = AgentMemory("p")
    assert shared.get_stats()["total_nodes"] == plain.get_stats()["total_nodes"]


def test_plain_save_over_a_live_store_is_refused_but_a_copy_is_fine(tmp_path):
    path = str(tmp_path / "p.memory")
    AgentMemory.open(path).remember("convention", {"name": "n", "rule": "r"})
    loaded = AgentMemory.load(path)
    with pytest.raises(StoreInUse):
        loaded.save(path)
    loaded.save(str(tmp_path / "copy.memory"))
    assert AgentMemory.load(str(tmp_path / "copy.memory")).get_stats()["total_nodes"] >= 1


def test_shared_save_to_its_own_path_is_a_checkpoint(tmp_path):
    path = str(tmp_path / "p.memory")
    mem = AgentMemory.open(path)
    mem.remember("convention", {"name": "n", "rule": "r"})
    assert os.path.getsize(path + ".journal") > 100
    mem.save(path)
    header_only = len(open(path + ".journal", "rb").read().splitlines()) == 1
    assert header_only
    mem.save(str(tmp_path / "copy.memory"))
    assert (tmp_path / "copy.memory").exists()


def test_close_and_context_manager(tmp_path):
    path = str(tmp_path / "p.memory")
    with AgentMemory.open(path) as mem:
        mem.remember("convention", {"name": "n", "rule": "r"})
    with pytest.raises(ValueError):
        mem.remember("convention", {"name": "n2", "rule": "r2"})
    assert AgentMemory.open(path).get_stats()["total_nodes"] >= 2


def test_options_are_forwarded_and_validated(tmp_path):
    AgentMemory.open(str(tmp_path / "a.memory"), durability="fsync", lock_timeout=2.0)
    with pytest.raises(ValueError):
        AgentMemory.open(str(tmp_path / "b.memory"), durability="sometimes")


def test_plain_memory_is_unchanged(tmp_path):
    mem = AgentMemory("t")
    assert mem.is_shared is False and mem.refresh() == 0 and mem.checkpoint() is False
    mem.close()                                              # harmless
    path = str(tmp_path / "p.memory")
    mem.remember("convention", {"name": "n", "rule": "r"})
    mem.save(path)
    assert AgentMemory.load(path).get_stats()["total_nodes"] == mem.get_stats()["total_nodes"]
```

- [ ] **Step 2: Run to verify failure** — FAIL (`AgentMemory` has no `open`).

- [ ] **Step 3: Implement** in `src/agent_memory/memory.py`:

Imports to add: `import os`, `from .storage import JournalBackend, StoreInUse, atomic_write_text` (import `StoreInUse` from `.storage.errors` if not re-exported).

In `__init__` set `self._backend = None` before `_bind`. In `load()` set `memory._backend = None` and replace `graph = ISONGraph.load(path)` with:

```python
        if os.path.exists(str(path) + ".journal"):
            graph = JournalBackend.read_only(path)        # snapshot + journal, as a plain graph
        else:
            graph = SharedGraph.from_ison(Path(path).read_text(encoding="utf-8"), Path(path).stem)
```
(`SharedGraph` imported from `.storage`. Keep the existing `.isonl` behaviour: if the suffix is `.isonl`, keep using `ISONGraph.load(path)`.)

Add:

```python
    @classmethod
    def open(cls, path, repo_root: Optional[str] = None, agent: str = "unknown",
             **backend_options) -> "AgentMemory":
        """Open (or create) a store that several processes can share safely.

        Every write is durable when the call returns and visible to other processes on their
        next read. Options: durability ("flush" | "fsync"), lock_timeout, compact_min_bytes.
        """
        memory = cls.__new__(cls)
        memory._open_store(path, repo_root, agent, **backend_options)
        return memory

    def _open_store(self, path, repo_root, agent, **backend_options) -> None:
        backend = JournalBackend(path, agent=agent, **backend_options)
        graph = backend.open()
        self.name = graph.name
        self.repo_root = str(repo_root) if repo_root else None
        self.load_warnings = list(backend.warnings)
        self._backend = backend
        self._bind(graph, agent)
        with graph.transaction():
            if not graph.has_node("agent", "self"):
                graph.add_node("agent", "self", name="agent")

    @property
    def is_shared(self) -> bool:
        return self._backend is not None

    def refresh(self) -> int:
        """Apply what other processes wrote since this one last looked."""
        return self._backend.refresh() if self._backend is not None else 0

    def checkpoint(self) -> bool:
        """Fold the journal into the snapshot. False when not shared or it was skipped."""
        return self._backend.checkpoint() if self._backend is not None else False

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()

    def __enter__(self) -> "AgentMemory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _sync(self) -> None:
        if self._backend is not None:
            self._backend.refresh_if_stale()
```

Replace `save` with:

```python
    def save(self, path: str) -> None:
        """Save memory to file (atomic). In shared mode, saving to the store's own path is a
        checkpoint; saving elsewhere writes a full snapshot copy. A plain instance may not
        overwrite a store that has a journal: open it with AgentMemory.open()."""
        target = str(path)
        if self._backend is not None and os.path.normcase(os.path.abspath(target)) == \
                os.path.normcase(self._backend.path):
            self._backend.checkpoint()
            return
        if self._backend is None and os.path.exists(target + ".journal"):
            raise StoreInUse(f"{target} is a shared store (it has a journal); use "
                             f"AgentMemory.open({target!r}) or save to another path")
        text = self.graph.to_isonl() if Path(target).suffix == ".isonl" else self.graph.to_ison()
        atomic_write_text(target, text)
```

Call `self._sync()` first in `get_context`, `recall`, `check`, `check_suggestion`, `who_knows`, `get_stats`. In `__init__.py` export `StoreInUse, StoreLockTimeout, StoreCorrupt, AgentMemoryError` (add to `__all__`).

- [ ] **Step 4: Run** — `python -m pytest tests/test_shared_memory.py -q` → 8 passed; full suite green (265 + 8 + earlier backend tests; the existing 258 unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory tests/test_shared_memory.py
git commit -m "feat: AgentMemory.open for shared stores; load replays the journal; save is atomic"
```

---

### Task 7: Transactions around every read-modify-write; fresh caches

**Files:**
- Create: `src/agent_memory/storage/txn.py`, `tests/test_shared_writes.py`
- Modify: `src/agent_memory/writer.py`, `src/agent_memory/anchors.py`, `src/agent_memory/consolidation.py`, `src/agent_memory/memory.py`, `src/agent_memory/stores/base.py`, `src/agent_memory/storage/__init__.py`

**Interfaces:**
- Produces: `txn(graph)` (a transaction on a shared graph, a no-op context otherwise); `transactional` decorator (wraps a method in `txn(self.graph)`, sets `__transactional__ = True`); every public method of every `BaseStore` subclass is transactional (generators excepted); the writer drops its text index when `graph.remote_ops` changes.

- [ ] **Step 1: Write the failing tests** — `tests/test_shared_writes.py`

```python
import inspect
import re
from pathlib import Path

import pytest

from agent_memory import AgentMemory
from agent_memory.stores.base import BaseStore


def two(tmp_path):
    path = str(tmp_path / "p.memory")
    return AgentMemory.open(path, agent="a"), AgentMemory.open(path, agent="b")


def active(mem, kind):
    return [n for n in mem.graph.nodes(kind) if n.properties.get("status") == "active"]


def test_two_instances_same_fact_merge_not_duplicate(tmp_path):
    a, b = two(tmp_path)
    fields = {"name": "naming", "rule": "cache keys use colon namespaces"}
    first = a.remember("convention", fields)
    second = b.remember("convention", fields)
    assert first.created and second.merged and second.id == first.id
    assert len(active(b, "convention")) == 1
    assert b.graph.get_node("convention", first.id).properties["confidence"] > 0.7


def test_remote_record_found_by_writer_index(tmp_path):
    a, b = two(tmp_path)
    b.remember("convention", {"name": "warm", "rule": "warms the writer index for this kind"})
    a.remember("convention", {"name": "late", "rule": "written by the other process"})
    result = b.remember("convention", {"name": "late", "rule": "written by the other process"})
    assert result.merged and len(active(b, "convention")) == 2


def test_feedback_counters_are_not_lost(tmp_path):
    a, b = two(tmp_path)
    rec = a.remember("convention", {"name": "n", "rule": "count me"})
    for _ in range(10):
        a.feedback(rec.id, True)
        b.feedback(rec.id, True)
    a.refresh()
    assert a.graph.get_node("convention", rec.id).properties["helpful"] == 20


def test_supersede_across_instances(tmp_path):
    a, b = two(tmp_path)
    old = a.remember("convention", {"name": "n", "rule": "old rule"})
    new = b.supersede(old.id, "convention", {"name": "n", "rule": "new rule"})
    a.refresh()
    assert a.graph.get_node("convention", old.id).properties["status"] == "superseded"
    assert a.graph.has_edge("SUPERSEDES", new.ref, old.ref)


def test_a_session_started_by_one_agent_is_ended_by_another(tmp_path):
    a, b = two(tmp_path)
    sid = a.start_session("add retry logic")
    b.end_session(sid, completion=1.0)
    a.refresh()
    props = a.graph.get_node("session", sid).properties
    assert props.get("ended") and float(props["completion"]) == 1.0


def test_anchor_and_recall_across_instances(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "cache.py").write_text("x = 1\n")
    path = str(tmp_path / "p.memory")
    a = AgentMemory.open(path, repo_root=str(repo), agent="a")
    b = AgentMemory.open(path, repo_root=str(repo), agent="b")
    a.remember("convention", {"name": "n", "rule": "cache keys use colon namespaces"},
               anchors=["src/cache.py"])
    got = b.recall(task="cache keys", files=["src/cache.py"])
    assert got.items and got.items[0].staleness == "fresh"


def test_every_public_store_method_is_transactional():
    stores = [cls for _, cls in AgentMemory._STORES]
    assert stores
    for cls in stores:
        for name, fn in vars(cls).items():
            if name.startswith("_") or not inspect.isfunction(fn):
                continue
            if inspect.isgeneratorfunction(getattr(fn, "__wrapped__", fn)):
                continue
            assert getattr(fn, "__transactional__", False), f"{cls.__name__}.{name}"


def test_plain_stores_still_work_without_a_shared_graph():
    mem = AgentMemory("t")
    mem.decisions.add_decision("Use Postgres", "transactions")
    assert mem.decisions.get_stats()


def test_nothing_mutates_node_properties_behind_the_graphs_back():
    pattern = re.compile(r"properties\[[^\]]+\]\s*(=|\+=|-=)|properties\.(update|pop|setdefault|clear)\(")
    offenders = []
    for path in (Path(__file__).resolve().parents[1] / "src").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}")
    assert offenders == []


def test_base_store_subclass_hook_skips_generators_and_privates():
    class Demo(BaseStore):
        def get_stats(self):
            return {}

        def lines(self):
            yield 1

        def _private(self):
            return 1

    assert getattr(Demo.get_stats, "__transactional__", False)
    assert not getattr(Demo.lines, "__transactional__", False)
    assert not getattr(Demo._private, "__transactional__", False)
```

- [ ] **Step 2: Run to verify failure** — FAIL (`__transactional__` missing; duplicate-fact test fails).

- [ ] **Step 3: Implement**

`src/agent_memory/storage/txn.py`:

```python
"""Helpers that make store code indifferent to shared vs plain mode."""
from __future__ import annotations

import contextlib
import functools


def txn(graph):
    """A transaction on `graph` when it is shared; a do-nothing context otherwise."""
    factory = getattr(graph, "transaction", None)
    return factory() if factory is not None else contextlib.nullcontext()


def transactional(fn):
    """Run a method (on an object with a `.graph`) inside one transaction: fresh state in,
    one journal line out, and no check-then-act race."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with txn(self.graph):
            return fn(self, *args, **kwargs)
    wrapper.__transactional__ = True
    return wrapper
```

Export `txn`, `transactional` from `storage/__init__.py`.

`stores/base.py` — add `import inspect`, `from ..storage.txn import transactional`, and inside `BaseStore`:

```python
    def __init_subclass__(cls, **kwargs):
        """Every public store method runs in a transaction, so reads see other processes'
        writes and read-modify-write code cannot race. Generators and private helpers are left
        alone. With a plain graph the wrapper is a no-op."""
        super().__init_subclass__(**kwargs)
        for name, value in list(vars(cls).items()):
            if name.startswith("_") or not inspect.isfunction(value):
                continue
            if inspect.isgeneratorfunction(value) or getattr(value, "__transactional__", False):
                continue
            setattr(cls, name, transactional(value))
```

`writer.py` — import `from .storage.txn import transactional`; decorate `add`, `supersede`, `retract`, `feedback` with `@transactional`; add to `__init__`: `self._remote_seen = 0`; and at the top of `_active_with_text`:

```python
        remote = getattr(self.graph, "remote_ops", 0)
        if remote != self._remote_seen:           # other processes changed the graph
            self._by_text.clear()
            self._indexed.clear()
            self._remote_seen = remote
```

`anchors.py` — import `from .storage.txn import txn`; wrap the bodies of `add_anchor` and `restamp` in `with txn(graph):`.

`consolidation.py` — import `from .storage.txn import txn`; in `consolidate()` wrap the body in `with txn(self.memory.graph):`.

`memory.py` — import `transactional`; decorate `start_session`, `end_session`, `record`, `remember`, `supersede`, `retract`, `feedback`, `anchor`, `verify` with `@transactional`.

- [ ] **Step 4: Run** — `python -m pytest tests/test_shared_writes.py -q` → 10 passed; full suite green (the existing 258 unchanged — single-process behaviour must be identical). If any existing test fails, the wrapper changed behaviour for plain graphs; fix the wrapper, never the old test.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory tests/test_shared_writes.py
git commit -m "feat: run every write path in a transaction; refresh the writer index on remote changes"
```

---

### Task 8: Multi-process stress and crash injection

**Files:**
- Create: `tests/concurrency_worker.py`, `tests/test_concurrency.py`
- Modify: `pyproject.toml` (marker `stress`, `addopts = "-m 'not perf and not stress'"`)

**Interfaces:**
- Consumes: everything above. `tests/concurrency_worker.py` usage: `python concurrency_worker.py STORE WORKER_ID N_OPS ACK_DIR [--compact-bytes N] [--checkpoint-every K]`; it appends one JSON line to `ACK_DIR/ack-<id>.jsonl` **after** each acknowledged write.

- [ ] **Step 1: Write the worker** — `tests/concurrency_worker.py`

```python
"""One writer process for the concurrency tests."""
import json
import random
import sys
from pathlib import Path

from agent_memory import AgentMemory

SHARED = {"name": "shared", "rule": "every worker records this one fact"}


def main(argv):
    store, wid, n_ops, ack_dir = argv[0], int(argv[1]), int(argv[2]), Path(argv[3])
    options = {}
    checkpoint_every = 0
    rest = argv[4:]
    while rest:
        flag, value = rest[0], rest[1]
        if flag == "--compact-bytes":
            options["compact_min_bytes"] = int(value)
        elif flag == "--checkpoint-every":
            checkpoint_every = int(value)
        rest = rest[2:]
    memory = AgentMemory.open(store, agent=f"worker-{wid}", **options)
    ack = open(ack_dir / f"ack-{wid}.jsonl", "a", buffering=1, encoding="utf-8")
    rng = random.Random(wid)
    for i in range(n_ops):
        roll = rng.random()
        if roll < 0.45:
            res = memory.remember("convention",
                                  {"name": f"w{wid}-{i}", "rule": f"worker {wid} rule {i}"})
            ack.write(json.dumps({"op": "add", "id": res.id}) + "\n")
        elif roll < 0.65:
            res = memory.remember("convention", SHARED)
            ack.write(json.dumps({"op": "shared", "id": res.id}) + "\n")
        elif roll < 0.85:
            res = memory.remember("convention", SHARED)
            memory.feedback(res.id, True)
            ack.write(json.dumps({"op": "helpful", "id": res.id}) + "\n")
        else:
            memory.recall(task=f"worker {rng.randrange(max(n_ops, 1))} rule")
        if checkpoint_every and (i + 1) % checkpoint_every == 0:
            memory.checkpoint()
    memory.refresh()
    if wid % 2 == 0:
        memory.close()


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 2: Write the tests** — `tests/test_concurrency.py`

```python
import json
import threading
import time
from pathlib import Path

import pytest
from ison_graph import ISONGraph

from agent_memory import AgentMemory
from agent_memory.storage import JournalBackend, journal
from storage_helpers import TESTS, child_env

import subprocess
import sys

WORKER = str(Path(TESTS) / "concurrency_worker.py")


def spawn(store, wid, n_ops, ack_dir, *extra, env=None):
    return subprocess.Popen([sys.executable, WORKER, str(store), str(wid), str(n_ops),
                             str(ack_dir), *map(str, extra)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=child_env(env))


def acked(ack_dir):
    rows = []
    for path in Path(ack_dir).glob("ack-*.jsonl"):
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return rows


def state_of(graph):
    nodes = sorted((n.type, str(n.id), json.dumps(dict(n.properties), sort_keys=True, default=str))
                   for n in graph.nodes() if n.type != "store_meta")
    edges = sorted((e.rel_type, str(e.source), str(e.target)) for e in graph.edges())
    return nodes, edges


def check_store(path, ack_dir, allow_unacked=False):
    """The invariants every run must keep."""
    mem = AgentMemory.open(str(path))
    rows = acked(ack_dir)
    present = {n.id for n in mem.graph.nodes("convention")}
    for row in rows:
        assert row["id"] in present, f"acknowledged write lost: {row}"
    shared = [n for n in mem.graph.nodes("convention")
              if n.properties.get("name") == "shared" and n.properties.get("status") == "active"]
    assert len(shared) <= 1, "the shared fact was duplicated"
    if shared and not allow_unacked:
        assert shared[0].properties["helpful"] == sum(1 for r in rows if r["op"] == "helpful")
    names = [n.properties.get("name") for n in mem.graph.nodes("convention")
             if n.properties.get("name") != "shared"]
    assert len(names) == len(set(names)), "a per-worker fact was duplicated"
    # compare through a text round trip, so benign type normalisation (a list stored as text)
    # cannot make an in-memory graph look different from the same graph reloaded
    before = state_of(ISONGraph.from_ison(mem.graph.to_ison(), "x"))
    mem.checkpoint()
    assert state_of(ISONGraph.load(path)) == before, "snapshot and journal disagree"
    assert journal.read_tail(str(path) + ".journal", 0).corrupt_at is None
    return mem


def run_workers(tmp_path, workers, n_ops, *extra):
    store, ack = tmp_path / "p.memory", tmp_path / "ack"
    ack.mkdir()
    procs = [spawn(store, wid, n_ops, ack, *extra) for wid in range(workers)]
    for proc in procs:
        out, err = proc.communicate(timeout=300)
        assert proc.returncode == 0, err
    return store, ack


def test_processes_writing_at_once_lose_nothing(tmp_path):
    store, ack = run_workers(tmp_path, 5, 40, "--compact-bytes", 2000)
    check_store(store, ack)


@pytest.mark.stress
def test_heavy_stress(tmp_path):
    store, ack = run_workers(tmp_path, 8, 200, "--compact-bytes", 20000,
                             "--checkpoint-every", 50)
    check_store(store, ack)


@pytest.mark.parametrize("point,after", [
    ("before_append", 7), ("mid_append", 7), ("after_append", 7),
    ("before_snapshot_replace", 1), ("after_snapshot_replace", 1)])
def test_a_process_killed_at_any_point_leaves_a_usable_store(tmp_path, point, after):
    store, ack = tmp_path / "p.memory", tmp_path / "ack"
    ack.mkdir()
    crasher = spawn(store, 1, 60, ack, "--checkpoint-every", 5,
                    env={"AGENT_MEMORY_FAULT": point, "AGENT_MEMORY_FAULT_AFTER": str(after)})
    crasher.communicate(timeout=120)
    assert crasher.returncode == 86, "the fault never fired"
    check_store(store, ack, allow_unacked=True)
    survivor = spawn(store, 2, 30, ack, "--checkpoint-every", 7)      # the store still works
    _, err = survivor.communicate(timeout=120)
    assert survivor.returncode == 0, err
    check_store(store, ack, allow_unacked=True)


def test_a_crash_among_live_writers_does_not_hurt_them(tmp_path):
    store, ack = tmp_path / "p.memory", tmp_path / "ack"
    ack.mkdir()
    procs = [spawn(store, 1, 60, ack, env={"AGENT_MEMORY_FAULT": "mid_append",
                                           "AGENT_MEMORY_FAULT_AFTER": "9"})]
    procs += [spawn(store, wid, 40, ack, "--checkpoint-every", 6) for wid in (2, 3, 4)]
    codes = []
    for proc in procs:
        _, err = proc.communicate(timeout=300)
        codes.append(proc.returncode)
        if proc.returncode not in (0, 86):
            pytest.fail(err)
    assert codes[0] == 86 and codes[1:] == [0, 0, 0]
    check_store(store, ack, allow_unacked=True)


def test_readers_survive_compaction(tmp_path):
    path = str(tmp_path / "p.memory")
    writer = AgentMemory.open(path, agent="w")
    reader = AgentMemory.open(path, agent="r")
    stop, errors, counts = threading.Event(), [], []

    def read():
        try:
            while not stop.is_set():
                reader.recall(task="rule number")
                counts.append(sum(1 for _ in reader.graph.nodes("convention")))
        except Exception as exc:               # any exception here is a failure
            errors.append(repr(exc))

    thread = threading.Thread(target=read)
    thread.start()
    for i in range(80):
        writer.remember("convention", {"name": f"n{i}", "rule": f"rule number {i}"})
        if i % 5 == 0:
            writer.checkpoint()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=30)
    assert errors == []
    assert counts == sorted(counts), "the reader saw records disappear"
    reader.refresh()
    assert sum(1 for _ in reader.graph.nodes("convention")) == 80
```

`pyproject.toml`: add `"stress: heavy multi-process stress (opt in with -m stress)"` to `markers` and change `addopts` to `"-m 'not perf and not stress'"`.

- [ ] **Step 3: Run** — `python -m pytest tests/test_concurrency.py -q` → expect 8 passed in under ~60 s. Any failure here is a real defect in Tasks 2–7: **diagnose the cause and fix it in the right module**; do not loosen an assertion. Then run `python -m pytest tests/test_concurrency.py -q -m stress` once and the file three more times to shake out timing issues. Finally the full suite.

- [ ] **Step 4: Commit**

```bash
git add tests/concurrency_worker.py tests/test_concurrency.py pyproject.toml
git commit -m "test: multi-process stress and crash-injection harness for the shared store"
```

---

### Task 9: Serial equivalence, performance gates, documentation

**Files:**
- Create: `tests/shared_mode.py`, `tests/test_storage_perf.py`
- Modify: `tests/conftest.py`, `tests/eval/run.py`, `README.md`, `DESCRIBE.md`, `docs/superpowers/specs/2026-11-17-concurrency-safe-storage-design.md` (new section 12), `pyproject.toml` (nothing new if Task 8 added the marker)

**Interfaces:**
- Produces: `tests/shared_mode.py::force_shared(tmp_dir)` — a context manager that patches `AgentMemory.__init__` so every `AgentMemory(name, ...)` is backed by a fresh shared store under `tmp_dir`; env switch `AGENT_MEMORY_FORCE_SHARED=1` applies it to the whole pytest run; `python -m tests.eval.run --shared` runs the evaluation through shared mode.

- [ ] **Step 1: Serial-equivalence harness**

`tests/shared_mode.py`:

```python
"""Run code written for plain AgentMemory(...) against a shared store instead."""
import contextlib
import itertools
import os
import tempfile

from agent_memory import AgentMemory

_counter = itertools.count()


@contextlib.contextmanager
def force_shared(directory=None):
    own = directory is None
    directory = tempfile.mkdtemp(prefix="agent-memory-shared-") if own else str(directory)
    original = AgentMemory.__init__

    def init(self, name="project", repo_root=None, agent="unknown"):
        path = os.path.join(directory, f"{name}-{next(_counter)}.memory")
        self._open_store(path, repo_root, agent)

    AgentMemory.__init__ = init
    try:
        yield directory
    finally:
        AgentMemory.__init__ = original
```

Add to `tests/conftest.py`:

```python
import os

import pytest


@pytest.fixture(autouse=True)
def _force_shared_mode(tmp_path):
    if os.environ.get("AGENT_MEMORY_FORCE_SHARED") != "1":
        yield
        return
    from shared_mode import force_shared
    with force_shared(tmp_path):
        yield
```

Read `tests/eval/run.py` and add a `--shared` flag that wraps the run in `force_shared()`.

- [ ] **Step 2: Run the equivalence checks and record them**
  - `AGENT_MEMORY_FORCE_SHARED=1 python -m pytest tests -q` — the whole existing suite through shared mode. Every failure is either (a) a real defect (fix it in the right module) or (b) a test that legitimately inspects plain-mode internals (e.g. asserts on raw node counts including the meta node, or on a file the shared store does not write). For (b), do **not** edit the test; list each one in the report with its reason. Expected: (b) is a short list, ideally empty.
  - `python -m tests.eval.run` and `python -m tests.eval.run --shared`: the printed scorecards must be **identical** (recall 1.00, leaks 0, abstention 1.00, held-out abstention 0.38). Save both outputs to the report.

- [ ] **Step 3: Performance gates** — `tests/test_storage_perf.py`

```python
import statistics
import subprocess
import sys
import time

import pytest

from agent_memory import AgentMemory
from storage_helpers import child_env

TIMER = """
import json, sys, time
from agent_memory import AgentMemory
mem = AgentMemory.open(sys.argv[1], agent=sys.argv[2])
lat = []
for i in range(int(sys.argv[3])):
    t = time.perf_counter()
    mem.remember("convention", {"name": f"{sys.argv[2]}-{i}", "rule": f"rule {sys.argv[2]} {i}"})
    lat.append((time.perf_counter() - t) * 1000)
print(json.dumps(lat))
"""


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def test_always_on_guard_single_writer_is_not_pathological(tmp_path):
    mem = AgentMemory.open(str(tmp_path / "p.memory"))
    best = min(_time_writes(mem, 30, tag) for tag in "abc")
    assert best < 5.0, f"30 writes took {best:.2f}s"          # generous: tens of ms each at worst


def _time_writes(mem, n, tag):
    start = time.perf_counter()
    for i in range(n):
        mem.remember("convention", {"name": f"{tag}{i}", "rule": f"guard rule {tag} {i}"})
    return time.perf_counter() - start


def test_always_on_guard_idle_refresh_is_cheap(tmp_path):
    mem = AgentMemory.open(str(tmp_path / "p.memory"))
    start = time.perf_counter()
    for _ in range(500):
        mem._sync()
    assert (time.perf_counter() - start) / 500 < 0.005


@pytest.mark.perf
def test_write_p95_under_contention(tmp_path):
    path = str(tmp_path / "p.memory")
    AgentMemory.open(path).close()
    procs = [subprocess.Popen([sys.executable, "-c", TIMER, path, f"p{i}", "60"],
                              stdout=subprocess.PIPE, text=True, env=child_env())
             for i in range(4)]
    latencies = []
    for proc in procs:
        out, _ = proc.communicate(timeout=300)
        latencies += __import__("json").loads(out)
    assert percentile(latencies, 0.95) < 15.0


@pytest.mark.perf
def test_idle_refresh_is_under_a_fifth_of_a_millisecond(tmp_path):
    mem = AgentMemory.open(str(tmp_path / "p.memory"))
    start = time.perf_counter()
    for _ in range(2000):
        mem._sync()
    assert (time.perf_counter() - start) / 2000 < 0.0002


@pytest.mark.perf
def test_open_a_5000_record_store_in_under_1_5_seconds(tmp_path):
    path = str(tmp_path / "p.memory")
    mem = AgentMemory.open(path, compact_min_bytes=1 << 40)
    with mem.graph.transaction():
        for i in range(5000):
            mem.remember("convention", {"name": f"n{i}", "rule": f"bulk rule number {i}"})
    mem.close()
    start = time.perf_counter()
    AgentMemory.open(path)
    assert time.perf_counter() - start < 1.5
```

Run `python -m pytest tests/test_storage_perf.py -q` (guards) and `-m perf` once on a cool machine; a perf-only failure on this throttled laptop is reported with its numbers, not "fixed" by loosening the budget.

- [ ] **Step 4: Documentation**
  - `README.md`: a "Sharing one memory between agents" section with the `open()` example from spec §5, the guarantees list, the file trio, `durability`, `checkpoint`/`close`, what is unsupported (network filesystems), and the CLI/server note (sub-project 3). Every snippet must be executed.
  - `DESCRIBE.md`: replace the limitation "Concurrent writers are not yet safe…" with an accurate one (several processes on one machine are safe through `open()`; plain `load`/`save` stays single-process; no network filesystems); update the Maturity paragraph with the real numbers from the final `python -m pytest tests` run (and the opt-in stress/perf markers).
  - Spec section 12 "Implementation notes": the deviations — `BaseStore` auto-wrapping (ruling above), the writer index invalidation via `remote_ops`, lockless reads never declare corruption, stale-temp-file age, `read_only`, and anything Task 8/9 found.

- [ ] **Step 5: Full verification and commit**
  - `python -m pytest tests -q` green; `AGENT_MEMORY_FORCE_SHARED=1 python -m pytest tests -q` result recorded; `python -m pytest tests/test_concurrency.py -q -m stress` green.

```bash
git add tests README.md DESCRIBE.md docs
git commit -m "test: shared-mode equivalence, performance gates; docs for concurrent use"
```
