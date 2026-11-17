"""
Anchors tie a memory to files. Each ANCHORED_TO edge stores the content
hash the memory was written against, so staleness can be computed against
the working tree at read time. Staleness is never stored.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from .record import NodeRef

EDGE = "ANCHORED_TO"
STALENESS_FACTOR = {"fresh": 1.0, "unknown": 1.0, "drifted": 0.7, "broken": 0.4, "orphaned": 0.2}
_SEVERITY = {"fresh": 0, "drifted": 1, "broken": 2, "orphaned": 3}
# Files larger than this are never read or hashed: an anchor to one has no hash and its
# state is "unknown". Bounds the work (and memory) one recall can spend on a single file.
MAX_HASH_BYTES = 2 * 1024 * 1024
_TOO_BIG = object()
OUTSIDE = "outside"


def norm_path(path, repo_root: Optional[str] = None) -> str:
    """Repo-relative POSIX path. Paths outside the repo are returned as given, with forward slashes."""
    raw = str(path)
    posix = raw.replace("\\", "/")
    if repo_root and os.path.isabs(raw):
        root = str(Path(repo_root).resolve()).replace("\\", "/").rstrip("/")
        full = str(Path(raw).resolve()).replace("\\", "/")
        if full.lower().startswith(root.lower() + "/"):
            return full[len(root) + 1:]
        return full
    while posix.startswith("./"):
        posix = posix[2:]
    return posix


def _is_absolute(path: str) -> bool:
    return os.path.isabs(path) or path.startswith("/") or bool(re.match(r"[A-Za-z]:", path))


def anchor_path(path, repo_root: Optional[str] = None) -> Tuple[str, bool]:
    """(the path an anchor stores, whether it is inside the repo).

    Inside: the repo-relative POSIX path. A relative path that leaves the repo
    ("../x.py", "a/../../x.py", judged lexically) is kept as given. An absolute path
    outside the repo is never stored: it becomes "outside/<file name>", so no user name
    or machine layout reaches a memory file meant to be committed.
    """
    rel = norm_path(path, repo_root)
    if _is_absolute(rel):
        name = rel.rstrip("/").rsplit("/", 1)[-1]
        return f"{OUTSIDE}/{name}", False
    lexical = posixpath.normpath(rel) if rel else rel
    return rel, not (lexical == ".." or lexical.startswith("../"))


def file_node_id(path: str) -> str:
    """Node ids may not contain whitespace, ':' or '|'."""
    return quote(path, safe="/._-")


def _normalised_bytes(data: bytes) -> bytes:
    lines = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    return b"\n".join(line.rstrip() for line in lines).rstrip(b"\n")


def _read(abs_path):
    """File bytes; None when missing or unreadable; _TOO_BIG over MAX_HASH_BYTES."""
    try:
        p = Path(abs_path)
        if p.stat().st_size > MAX_HASH_BYTES:
            return _TOO_BIG
        return p.read_bytes()
    except (OSError, ValueError):     # ValueError: a NUL byte in the path
        return None


def content_hash(abs_path) -> Optional[str]:
    """Hash of the normalised file content; None when missing, unreadable or over the cap."""
    data = _read(abs_path)
    if data is None or data is _TOO_BIG:
        return None
    return hashlib.sha1(_normalised_bytes(data)).hexdigest()


def head_commit(repo_root: Optional[str]) -> str:
    """HEAD commit read straight from .git, or '' when unavailable."""
    if not repo_root:
        return ""
    try:
        git = Path(repo_root) / ".git"
        if git.is_file():
            pointer = git.read_text(encoding="utf-8").split(":", 1)[1].strip()
            git = (Path(repo_root) / pointer).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        ref = head[4:].strip()
        ref_file = git / ref
        if ref_file.exists():
            return ref_file.read_text(encoding="utf-8").strip()
        packed = git / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0]
    except (OSError, IndexError, UnicodeDecodeError):
        pass
    return ""


def _abs(path: str, repo_root: str) -> Path:
    return Path(path) if os.path.isabs(path) else Path(repo_root) / path


def add_anchor(graph, ref: NodeRef, path, symbol: str = "",
               repo_root: Optional[str] = None, commit: str = "") -> None:
    """Anchor `ref` to `path`, stamping the current hash. Re-stamps an existing anchor.

    A path outside the repo (see anchor_path) is recorded without a hash, so its state
    is "unknown" and the file is never read; so is an unreadable or oversized file.
    """
    rel, inside = anchor_path(path, repo_root)
    target = ("file", file_node_id(rel))
    if not graph.has_node(*target):
        graph.add_node("file", target[1], path=rel)
    digest = content_hash(_abs(rel, repo_root)) if repo_root and inside else None
    if graph.has_edge(EDGE, ref, target):
        if not symbol:
            symbol = str(graph.get_edge(EDGE, ref, target).properties.get("symbol") or "")
        graph.remove_edge(EDGE, ref, target)
    graph.add_edge(EDGE, ref, target, symbol=symbol, hash=digest or "", commit=commit)


def _anchors(graph, ref: NodeRef) -> List[Tuple[str, str, str]]:
    out = []
    for edge in graph.edges(EDGE, source=ref):
        if not graph.has_node(*edge.target):
            continue
        path = str(graph.get_node(*edge.target).properties.get("path") or "")
        out.append((path, str(edge.properties.get("symbol") or ""), str(edge.properties.get("hash") or "")))
    return out


def anchor_paths(graph, ref: NodeRef) -> List[str]:
    return [path for path, _, _ in _anchors(graph, ref)]


def restamp(graph, ref: NodeRef, repo_root: Optional[str]) -> int:
    """Re-record every anchor of `ref` at the current file hash. Returns the count re-stamped.

    Anchors whose file is missing or unreadable are left untouched, so they stay orphaned.
    Anchors outside the repo are never hashed (see add_anchor).
    """
    if not repo_root:
        return 0
    commit = head_commit(repo_root)
    count = 0
    for path, symbol, _ in _anchors(graph, ref):
        if not anchor_path(path, repo_root)[1] or content_hash(_abs(path, repo_root)) is None:
            continue
        add_anchor(graph, ref, path, symbol, repo_root, commit)
        count += 1
    return count


class HashCache:
    """Per-call cache of file hash and text, so a file is read once per recall.

    get() returns (None, "") for a missing or unreadable file and ("", "") for a file over
    MAX_HASH_BYTES, which is not read at all.
    """

    def __init__(self, repo_root: Optional[str]):
        self.repo_root = repo_root
        self._seen: Dict[str, Tuple[Optional[str], str]] = {}

    def get(self, path: str) -> Tuple[Optional[str], str]:
        if path not in self._seen:
            digest, text = None, ""
            data = _read(_abs(path, self.repo_root))
            if data is _TOO_BIG:
                digest = ""
            elif data is not None:
                digest = hashlib.sha1(_normalised_bytes(data)).hexdigest()
                text = data.decode("utf-8", errors="replace")
            self._seen[path] = (digest, text)
        return self._seen[path]


def staleness(graph, ref: NodeRef, cache: HashCache) -> str:
    """Worst state across the anchors of `ref`."""
    if not cache.repo_root:
        return "unknown"
    worst = None
    for path, symbol, stored in _anchors(graph, ref):
        if not stored:
            continue
        current, text = cache.get(path)
        if current == "":       # over MAX_HASH_BYTES: not read, state unknown
            continue
        if current is None:
            state = "orphaned"
        elif current == stored:
            state = "fresh"
        elif symbol and not re.search(r"\b" + re.escape(symbol) + r"\b", text):
            state = "broken"
        else:
            state = "drifted"
        if worst is None or _SEVERITY[state] > _SEVERITY[worst]:
            worst = state
    return worst or "unknown"
