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
