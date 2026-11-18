"""A planned file change: what it is, how to show it, how to apply it."""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from ..storage.atomic import atomic_write_text
from . import InstallError


def read_text(path: Path) -> Tuple[Optional[str], str]:
    """(text with LF newlines, the file's newline) or (None, "\\n") when the file is absent.

    A leading UTF-8 BOM stays in the text, so it is written back. A file that is not UTF-8, or
    that mixes line-ending styles, is refused: re-encoding it or rewriting every line ending
    would change text the install has no business touching.
    """
    if not path.exists():
        return None, "\n"
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise InstallError(f"cannot read {path}: {exc}") from None
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise InstallError(f"{path} is not valid UTF-8; convert it first") from None
    crlf = raw.count("\r\n")
    bare_lf = raw.count("\n") - crlf
    bare_cr = raw.replace("\r\n", "").count("\r")
    if bare_cr or (crlf and bare_lf):
        raise InstallError(f"{path} mixes line endings; make them consistent first so no line "
                           "is rewritten by surprise")
    return raw.replace("\r\n", "\n"), ("\r\n" if crlf else "\n")


@dataclass
class Change:
    path: Path
    old: Optional[str]
    new: str
    note: str = ""
    newline: str = "\n"

    @property
    def status(self) -> str:
        if self.old is None:
            return "create"
        return "unchanged" if self.old == self.new else "update"

    def diff(self) -> str:
        return "".join(difflib.unified_diff(
            _diff_lines(self.old or ""), _diff_lines(self.new),
            fromfile=f"a/{self.path.name}", tofile=f"b/{self.path.name}"))


def _diff_lines(text: str) -> List[str]:
    """Lines for a diff; a final line with no newline carries git's marker so the output is not
    run together (`-a+b`)."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n\\ No newline at end of file\n"
    return lines


def _real(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _inside(path: Path, root: str) -> bool:
    base = _real(Path(root))
    try:
        return os.path.commonpath([base, _real(path)]) == base
    except ValueError:                  # different drives
        return False


def apply_changes(changes: List[Change], root: Optional[str] = None) -> List[Change]:
    """Write every change that is not 'unchanged'. Returns the ones written.

    Refused before anything is written: two changes for the same file (the second would drop the
    first); with `root`, a path that resolves outside it; a file that differs from what its
    change was planned against (edited or created since it was read). Each write is atomic (temp
    file + replace), so no file is ever left half-written; if a write fails part-way the error
    names the failed file and the ones already written.
    """
    seen = {}
    for change in changes:
        key = _real(change.path)
        if key in seen:
            raise InstallError(f"two changes target {change.path}; the second would drop the first")
        seen[key] = change
        if root is not None and not _inside(change.path, root):
            raise InstallError(f"{change.path} is outside {root}; refusing to write it")
    pending = [change for change in changes if change.status != "unchanged"]
    for change in pending:
        current, _ = read_text(change.path)
        if current != change.old:
            raise InstallError(f"{change.path} changed since it was read; nothing was written, "
                               "run the install again")
    written: List[Change] = []
    for change in pending:
        try:
            change.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(change.path, change.new.replace("\n", change.newline))
        except OSError as exc:
            done = ", ".join(str(c.path) for c in written) or "none"
            raise InstallError(f"could not write {change.path} ({exc}); already written: {done}; "
                               "that file is unchanged") from None
        written.append(change)
    return written
