"""Run code written for plain AgentMemory(...) against a shared store instead."""
from __future__ import annotations

import contextlib
import itertools
import os
import tempfile

from agent_memory import AgentMemory

_counter = itertools.count()


@contextlib.contextmanager
def force_shared(directory=None):
    own = directory is None
    directory = tempfile.mkdtemp(prefix="agent-memory-shared-") if own else str(directory)
    original = AgentMemory.__init__

    def init(self, name="project", repo_root=None, agent="unknown"):
        # One folder per instance keeps the file stem, and so `memory.name`, equal to `name`.
        folder = os.path.join(directory, str(next(_counter)))
        os.makedirs(folder, exist_ok=True)
        self._open_store(os.path.join(folder, f"{name}.memory"), repo_root, agent)

    AgentMemory.__init__ = init
    try:
        yield directory
    finally:
        AgentMemory.__init__ = original
