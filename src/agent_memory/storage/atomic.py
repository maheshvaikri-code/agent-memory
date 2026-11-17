"""Write a text file so readers never see a half-written one."""
from __future__ import annotations

import os
import stat
import time
import uuid
from pathlib import Path
from typing import Optional, Union


def atomic_write_text(path: Union[str, "os.PathLike[str]"], text: str, *,
                      retry_seconds: float = 2.0, fsync: bool = True) -> None:
    """Write `text` (UTF-8, LF newlines) to `path` via a temp file in the same directory.

    A symlink is written through: the temp file goes beside its target and replaces the
    target, so the link survives (as it did with a plain write). The target's permission bits
    are copied onto the temp file before any text is written to it (not on Windows, where
    chmod only sets the read-only flag).

    On Windows `os.replace` raises PermissionError while another process has the target open;
    it is retried with backoff for `retry_seconds` and then re-raised. The previous file is
    never touched until the replace succeeds, and the temp file never outlives the call.
    """
    target = Path(os.path.realpath(os.fspath(path)))
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    mode = _permission_bits(target)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            if mode is not None:
                try:
                    os.chmod(tmp, mode)
                except OSError:
                    pass                # best effort: the write itself must not fail on it
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


def _permission_bits(target: Path) -> Optional[int]:
    """The existing target's permission bits, or None when there is nothing to copy."""
    if os.name == "nt":
        return None
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        return None
