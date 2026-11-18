"""A planned file change: what it is, how to show it, how to apply it."""
from __future__ import annotations

import difflib
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
            (self.old or "").splitlines(keepends=True), self.new.splitlines(keepends=True),
            fromfile=f"a/{self.path.name}", tofile=f"b/{self.path.name}"))


def apply_changes(changes: List[Change]) -> List[Change]:
    """Write every change that is not 'unchanged'. Returns the ones written.

    Every file is first re-read: if any differs from what the change was planned against (edited
    or created since), nothing is written. Each write is atomic (temp file + replace), so a
    crash leaves the original intact.
    """
    pending = [change for change in changes if change.status != "unchanged"]
    for change in pending:
        current, _ = read_text(change.path)
        if current != change.old:
            raise InstallError(f"{change.path} changed since it was read; nothing was written, "
                               "run the install again")
    written = []
    for change in pending:
        change.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(change.path, change.new.replace("\n", change.newline))
        written.append(change)
    return written
