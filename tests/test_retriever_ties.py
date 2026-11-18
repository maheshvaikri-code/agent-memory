"""Exactly tied notes keep one order. Recency used to read the clock once per record, so two
notes with equal scores differed by the time between the two reads and their order followed the
evaluation (dict, hash-seed) order instead of the documented id tie-break."""
import os
import subprocess
import sys
from datetime import datetime as real_datetime, timedelta

from agent_memory import AgentMemory
from agent_memory.retrieval import retriever as retriever_module
from storage_helpers import child_env

STAMP = "2026-11-01T10:00:00+00:00"


def tied_memory(root):
    memory = AgentMemory("ties", repo_root=str(root))
    ids = []
    for words in ("alpha bravo", "charlie delta", "echo foxtrot"):
        ids.append(memory.remember("convention",
                                   {"category": "naming", "rule": f"cache keys {words}"}).id)
    for ident in ids:            # identical timestamps, so every score factor is equal
        memory.graph.update_node("convention", ident, created_at=STAMP, updated_at=STAMP)
    return memory, ids


class TickingClock(real_datetime):
    """datetime whose now() moves one second forward on every call."""
    ticks = 0

    @classmethod
    def now(cls, tz=None):
        cls.ticks += 1
        return real_datetime(2026, 11, 17, tzinfo=tz) + timedelta(seconds=cls.ticks)


def test_tied_notes_score_exactly_equal_and_follow_the_id_tie_break(tmp_path, monkeypatch):
    memory, ids = tied_memory(tmp_path)
    monkeypatch.setattr(retriever_module, "datetime", TickingClock)
    orders = set()
    for _ in range(200):
        items = memory.recall(task="cache keys naming", max_tokens=4000).items
        tied = [i for i in items if str(i.record.id) in ids]
        assert len(tied) == 3
        assert len({i.score for i in tied}) == 1, [i.score for i in tied]
        orders.add(tuple(str(i.record.id) for i in tied))
    assert orders == {tuple(sorted(ids))}


CHILD = """
import sys
sys.path[:0] = [sys.argv[1]]
from pathlib import Path
from test_retriever_ties import tied_memory
memory, ids = tied_memory(Path(sys.argv[2]))
print(",".join(str(i.record.id) for i in memory.recall(task="cache keys naming").items))
"""


def test_the_order_does_not_depend_on_the_hash_seed(tmp_path):
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    seen = set()
    for seed in ("1", "2", "3"):
        root = tmp_path / f"s{seed}"
        root.mkdir()
        out = subprocess.run([sys.executable, "-c", CHILD, tests_dir, str(root)],
                             capture_output=True, text=True, timeout=60,
                             env=child_env({"PYTHONHASHSEED": seed}))
        assert out.returncode == 0, out.stderr
        seen.add(out.stdout.strip())
    assert len(seen) == 1, seen
