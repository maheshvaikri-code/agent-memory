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
