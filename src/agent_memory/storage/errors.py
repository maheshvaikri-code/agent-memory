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
