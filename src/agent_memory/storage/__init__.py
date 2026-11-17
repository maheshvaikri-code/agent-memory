"""Shared-store layer: lock, journal, shared graph, backend. Built up task by task."""
from .atomic import atomic_write_text
from .backend import Backend, JournalBackend
from .errors import AgentMemoryError, StoreCorrupt, StoreInUse, StoreLockTimeout
from .lock import FileLock
from .shared_graph import META_ID, META_TYPE, SharedGraph

__all__ = ["atomic_write_text", "AgentMemoryError", "Backend", "FileLock", "JournalBackend",
           "META_ID", "META_TYPE", "SharedGraph", "StoreCorrupt", "StoreInUse",
           "StoreLockTimeout"]
