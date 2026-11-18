"""Keep the memory directory out of version control."""
from __future__ import annotations


def ensure_ignored(existing: str, entry: str = ".agent-memory/") -> str:
    """`existing` with `entry` ignored: unchanged when it already is (with or without the
    trailing slash, anchored or not)."""
    bare = entry.rstrip("/")
    accepted = {entry, bare, "/" + entry.lstrip("/"), "/" + bare.lstrip("/")}
    lines = {line.strip() for line in existing.splitlines()}
    if accepted & lines:
        return existing
    if existing and not existing.endswith("\n"):
        existing += "\n"
    return existing + entry + "\n"
