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
