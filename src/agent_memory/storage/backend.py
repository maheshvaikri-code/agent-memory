"""JournalBackend: the snapshot + journal store behind a shared AgentMemory.

Every mutation happens inside `transaction()`: take the exclusive lock, apply whatever other
processes appended since we last looked, run the body, append one checksummed line, release.
So the journal is a serial history and absolute values are safe. Reads call
`refresh_if_stale()`, which is two stats and takes no lock.

Why a lockless read is safe: every transaction is numbered `seq = previous + 1` under the lock
and compaction keeps the numbering, so a checksum-valid line whose seq is exactly ours + 1 is
the genuine next transaction, whichever version of the file it was read from. Anything else (a
generation we do not hold, a gap, a damaged or vanished line, an unreadable file) makes the
lockless read give up and retry under the lock. There the snapshot and journal are the truth
and this process's memory is only a cache of them: it is rebuilt from disk when it no longer
lines up with the files, and corruption is declared only when the whole journal fails to read.
"""
from __future__ import annotations

import contextlib
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from ison_graph import ISONGraph

from ..record import utc_now
from . import faults, journal
from .atomic import atomic_write_text
from .errors import StoreCorrupt, StoreLockTimeout
from .lock import FileLock
from .shared_graph import META_ID, META_TYPE, SharedGraph

_TEMP_MAX_AGE = 60.0       # seconds before a stray temp snapshot is considered abandoned

_FileSignature = Optional[Tuple[int, int, int]]


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


def _exists(path: str) -> bool:
    """Like os.path.exists, but only a missing file is absent: an unreadable snapshot must never
    be mistaken for no snapshot (that would overwrite it with an empty store)."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return False
    return True


def _file_signature(path: str) -> _FileSignature:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


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
        # This process's side of the lock: one thread at a time touches the graph and the
        # journal position, lockless reads included. Always taken before the file lock.
        self._mutex = threading.RLock()
        self.graph: Optional[SharedGraph] = None
        self.warnings: List[str] = []
        self.generation = ""
        self.seq = 0
        self.offset = 0
        self._signature: Optional[Tuple[_FileSignature, _FileSignature]] = None
        self._needs_reset = False
        self._diverged = False     # memory may differ from disk; rebuild before the next use
        self._dirty = False
        self._depth = 0
        self._pending: List[str] = []
        self._closed = False

    @contextlib.contextmanager
    def _exclusive(self):
        timeout = self._lock.timeout
        if not self._mutex.acquire(timeout=timeout if timeout >= 0 else -1):
            raise StoreLockTimeout(f"could not take {self.lock_path} within {timeout}s "
                                   f"(another thread of this process holds it)")
        try:
            yield
        finally:
            self._mutex.release()

    # -- opening -----------------------------------------------------------

    def open(self) -> SharedGraph:
        with self._exclusive(), self._lock:
            self._remove_stale_temp_files()
            created = False
            if _exists(self.path):
                graph = self._read_snapshot()
                if self._meta(graph) is None:                  # a legacy file
                    ISONGraph.add_node(graph, META_TYPE, META_ID, generation=_generation(), seq=0)
                    self._write_snapshot(graph)
                    created = True
            else:
                graph = SharedGraph(name=_stem(self.path))
                ISONGraph.add_node(graph, META_TYPE, META_ID, generation=_generation(), seq=0)
                self._write_snapshot(graph)
                created = True
            self._adopt(graph)
            if created:            # no journal on disk can belong to a generation made just now
                self._reset_journal(self.generation)
            else:
                self._catch_up()
                self._prepare_append()
        graph._backend = self
        return graph

    @staticmethod
    def read_only(path, lock_timeout: float = 10.0) -> SharedGraph:
        """Snapshot plus the journal replayed, as a plain graph with no backend attached."""
        backend = JournalBackend(path, lock_timeout=lock_timeout)
        if not _exists(backend.path):
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

    def _meta(self, graph: SharedGraph) -> Optional[Tuple[str, int]]:
        if not graph.has_node(META_TYPE, META_ID):
            return None
        props = graph.get_node(META_TYPE, META_ID).properties
        try:
            return str(props.get("generation", "")), int(props.get("seq", 0))
        except (TypeError, ValueError) as exc:
            raise StoreCorrupt(f"snapshot {self.path} has a damaged {META_TYPE} node: "
                               f"{exc}") from exc

    def _write_snapshot(self, graph: SharedGraph) -> None:
        atomic_write_text(self.path, graph.to_ison(), retry_seconds=self.replace_retry_seconds)

    def _adopt(self, graph: SharedGraph) -> None:
        self.graph = graph
        self.generation, self.seq = self._meta(graph)
        self.offset = 0

    def _remove_stale_temp_files(self) -> None:
        base = Path(self.path)
        pattern = re.compile(r"\." + re.escape(base.name) + r"\.\d+\.[0-9a-f]{8}\.tmp\Z")
        try:
            names = os.listdir(base.parent)
        except OSError:
            return
        now = time.time()
        for name in names:
            if not pattern.match(name):
                continue
            tmp = base.parent / name
            try:
                if now - tmp.stat().st_mtime > _TEMP_MAX_AGE:
                    tmp.unlink()
            except OSError:
                pass

    def _stat(self) -> Tuple[_FileSignature, _FileSignature]:
        """What `refresh_if_stale` compares. The journal's size and mtime catch appends; the
        snapshot's identity catches a compaction even when the rewritten journal has the same
        size and mtime as before (mtime ticks are coarse: about 1 ms on NTFS)."""
        return _file_signature(self.journal_path), _file_signature(self.path)

    # -- reading the journal -----------------------------------------------

    def _catch_up(self) -> int:
        """Apply what other processes appended. Call with the lock held."""
        applied = self._apply_tail(journal.read_tail(self.journal_path, self.offset), True)
        self._signature = self._stat()        # exact: nobody writes while we hold the lock
        return applied

    def _apply_tail(self, tail: journal.Tail, authoritative: bool) -> Optional[int]:
        """Apply `tail`. Returns the number of transactions applied, or None when a lockless
        read hit something unexpected and the caller must retry under the lock."""
        if tail.error is not None:        # unreadable is never the same as absent
            if not authoritative:
                return None
            raise StoreCorrupt(f"journal {self.journal_path} cannot be read: {tail.error}")
        if tail.generation != self.generation:   # absent, unfinished, stale, or compacted
            if not authoritative:
                return None
            return self._follow_snapshot()
        problem = None
        if tail.corrupt_at is not None:
            problem = self._damage(tail.corrupt_at)
        elif tail.shrunk:
            problem = f"journal {self.journal_path} is shorter than byte {self.offset}"
        applied = 0
        for tx in tail.txs if problem is None else ():
            if tx["seq"] <= self.seq:      # applied by an earlier lockless read that gave up
                continue
            if tx["seq"] != self.seq + 1:
                problem = (f"journal {self.journal_path} sequence gap: expected "
                           f"{self.seq + 1}, found {tx['seq']}")
                break
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
        if problem is not None:
            if not authoritative:
                return None
            if self.offset == 0:          # this read covered the whole journal: it is damaged
                raise StoreCorrupt(problem)
            # Our position does not line up with the file any more, e.g. it was cut back after
            # we read a line whose append then failed. Rebuild from disk; if the journal really
            # is damaged, reading it from the start raises.
            return self._reload()
        self.offset = tail.valid_end
        self._needs_reset = False
        return applied

    def _damage(self, at: int) -> str:
        try:
            with open(self.journal_path, "rb") as handle:
                line = handle.read(at).count(b"\n") + 1
            where = f"line {line} (byte {at})"
        except OSError:
            where = f"byte {at}"
        return f"journal {self.journal_path} is damaged at {where}"

    def _follow_snapshot(self) -> int:
        """The journal does not continue our generation. Under the lock the snapshot decides:
        if we hold exactly its state, the journal is absent, unfinished or stale (a compaction
        crashed before rewriting it; everything in it is folded in) and the next writer resets
        it. Otherwise another process compacted, perhaps more than once: reload."""
        fresh = self._read_snapshot()
        if self._meta(fresh) == (self.generation, self.seq):
            self._needs_reset = True
            self.offset = 0
            return 0
        return self._reload(fresh)

    def _reload(self, fresh: Optional[SharedGraph] = None, *, note: bool = True) -> int:
        """Rebuild memory from disk: the snapshot, plus the journal when it continues it."""
        if fresh is None:
            fresh = self._read_snapshot()
        meta = self._meta(fresh)
        if meta is None:
            raise StoreCorrupt(f"snapshot {self.path} has no {META_TYPE} node: it was "
                               f"overwritten outside the store")
        tail = journal.read_tail(self.journal_path, 0)
        if tail.error is not None:
            raise StoreCorrupt(f"journal {self.journal_path} cannot be read: {tail.error}")
        if tail.generation == meta[0] and tail.corrupt_at is not None:
            raise StoreCorrupt(self._damage(tail.corrupt_at))    # before memory is touched
        if note and meta[0] == self.generation:
            self.warnings.append(f"journal {self.journal_path} was rewritten under this "
                                 f"process; its memory was rebuilt from disk")
        self.graph._replace_contents(fresh)
        self.generation, self.seq = meta
        self.offset = 0
        if tail.generation != self.generation:
            self._needs_reset = True
            return 0
        return self._apply_tail(tail, True)

    def refresh(self) -> int:
        with self._exclusive(), self._lock:
            self._heal()
            return self._catch_up()

    def refresh_if_stale(self) -> int:
        """Apply new journal content if there is any. Two stats when there is none; no lock."""
        with self._exclusive():
            if self._depth > 0 or self._closed:
                return 0
            if not self._diverged:
                signature = self._stat()               # taken before reading: conservative
                if signature == self._signature:
                    return 0
                applied = self._apply_tail(journal.read_tail(self.journal_path, self.offset),
                                           False)
                if applied is not None:
                    self._signature = signature
                    return applied
            return self.refresh()

    def _heal(self) -> None:
        """Call with the lock held. A failed commit could not rebuild memory: do it now."""
        if self._diverged:
            self._reload(note=False)
            self._diverged = False

    # -- writing -----------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self):
        with self._exclusive():
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
                self._heal()
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
        try:
            size: Optional[int] = os.path.getsize(self.journal_path)
        except FileNotFoundError:
            size = None
        if self._needs_reset or size is None:
            self._reset_journal(self.generation)
        elif size > self.offset:
            with open(self.journal_path, "r+b") as handle:
                handle.truncate(self.offset)
                handle.flush()
                os.fsync(handle.fileno())
            self._signature = self._stat()

    def _reset_journal(self, generation: str) -> None:
        header = journal.encode_header(generation)
        with open(self.journal_path, "wb") as handle:
            handle.write(header)
            handle.flush()
            os.fsync(handle.fileno())
        self.offset = len(header)
        self._needs_reset = False
        self._signature = self._stat()

    def _commit(self, ops: List[str]) -> None:
        seq = self.seq + 1
        faults.hit("before_append")
        try:
            line = journal.encode_tx(seq, self.agent, utc_now(), ops)
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
        self._signature = self._stat()
        self._dirty = True
        self._maybe_compact()

    def _maybe_compact(self) -> None:
        """Compaction after a commit is housekeeping. The transaction is already durable, so
        a failure here is a warning: it must not make the caller think the write failed."""
        if self.offset <= self.compact_min_bytes:
            return
        try:
            if self.offset > os.path.getsize(self.path):
                self._compact()
        except Exception as exc:
            self.warnings.append(f"compaction skipped: {exc}")

    def _resync(self) -> None:
        """A commit failed while we still hold the lock: cut whatever part of the line reached
        the journal and rebuild memory from disk, so this process never holds a change that
        others cannot see. If the rebuild fails it is retried before the next use."""
        try:
            if os.path.getsize(self.journal_path) > self.offset:
                with open(self.journal_path, "r+b") as handle:
                    handle.truncate(self.offset)
        except Exception as exc:          # then a complete line stays, and memory follows it
            self.warnings.append(f"a failed append could not be cut from the journal: {exc}")
        self._diverged = True
        try:
            self._reload(note=False)
            self._diverged = False
            self._signature = self._stat()
        except Exception as exc:           # never mask the original error
            self.warnings.append(f"resync after a failed append did not complete: {exc}")

    # -- compaction --------------------------------------------------------

    def checkpoint(self) -> bool:
        """Fold the journal into a new snapshot. False when it had to be skipped."""
        with self._exclusive():
            if self._depth > 0:
                raise RuntimeError("checkpoint() cannot run inside a transaction: the "
                                   "transaction's changes are not journaled yet")
            with self._lock:
                self._heal()
                self._catch_up()
                self._prepare_append()
                return self._compact()

    def _compact(self) -> bool:
        graph = self.graph
        old_generation, old_seq = self._meta(graph)
        new_generation = _generation()
        ISONGraph.update_node(graph, META_TYPE, META_ID, generation=new_generation, seq=self.seq)
        try:
            faults.hit("before_snapshot_replace")
            self._write_snapshot(graph)
        except BaseException as exc:
            ISONGraph.update_node(graph, META_TYPE, META_ID, generation=old_generation,
                                  seq=old_seq)
            if not isinstance(exc, OSError):
                raise
            # a third party holds the snapshot open (antivirus, editor), or the disk is full
            self.warnings.append(f"compaction skipped: {exc}")
            return False
        faults.hit("after_snapshot_replace")
        self.generation = new_generation
        self._reset_journal(new_generation)
        self._dirty = False
        return True

    def close(self) -> None:
        """Checkpoint if this process wrote, then refuse further writes. Every write is
        already durable in the journal, so a checkpoint that cannot get the lock in time is
        skipped with a warning; the next compaction folds the journal instead."""
        with self._exclusive():
            if self._closed:
                return
            try:
                if self._dirty:
                    try:
                        self.checkpoint()
                    except StoreLockTimeout as exc:
                        self.warnings.append(f"checkpoint at close skipped: {exc}")
            finally:
                self._closed = True
