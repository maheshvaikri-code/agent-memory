import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for entry in (str(HERE), str(HERE.parent / "src")):      # src ends up first, tests/ second
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)


@pytest.fixture(autouse=True)
def _no_developer_agent_memory_settings(monkeypatch):
    """The developer's own store, repo, agent name and fault injection never reach a test (nor
    the subprocesses it starts, which copy os.environ): a CLI test passing only --repo would
    otherwise write into the store AGENT_MEMORY_STORE names. A test that needs one sets it
    itself with monkeypatch.setenv. AGENT_MEMORY_FORCE_SHARED is the suite's own mode switch
    and is left alone."""
    for name in ("AGENT_MEMORY_STORE", "AGENT_MEMORY_REPO", "AGENT_MEMORY_AGENT",
                 "AGENT_MEMORY_FAULT", "AGENT_MEMORY_FAULT_AFTER"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _force_shared_mode(tmp_path_factory):
    """AGENT_MEMORY_FORCE_SHARED=1 runs every test with AgentMemory(...) backed by a shared
    store: the serial-equivalence check of the concurrency-safe storage. The store files go in
    their own folder, not the test's tmp_path, which some tests list."""
    if os.environ.get("AGENT_MEMORY_FORCE_SHARED") != "1":
        yield
        return
    from shared_mode import force_shared
    with force_shared(tmp_path_factory.mktemp("shared-store")):
        yield
