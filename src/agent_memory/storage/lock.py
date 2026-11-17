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
