"""The spec's recall budget: p95 under 50 ms on a 5,000-record store, through MemoryService.

The strict test is opt-in (`python -m pytest tests/test_service_perf.py -m perf`): building
5,000 records takes minutes and a throttled CPU misses wall-clock bounds on unchanged code. The
always-on guard is loose and only catches a pathological (e.g. quadratic) regression."""
import statistics
import time

import pytest

from agent_memory.service import MemoryService, resolve_config

TASKS = ["add retry logic to the payment webhook", "how do we name cache keys",
         "migrate the ledger table", "fix the flaky indexer test", "token refresh on expiry"]


def build(tmp_path, n):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    service = MemoryService(resolve_config(repo=str(root), env={}))
    with service.memory.graph.transaction():
        for i in range(n):
            service.remember("convention", {"category": f"area{i % 40}",
                                            "rule": f"rule {i} about {TASKS[i % 5]} number {i}"})
    return service


def timings(service, rounds):
    out = []
    for i in range(rounds):
        start = time.perf_counter()
        service.recall(TASKS[i % 5])
        out.append((time.perf_counter() - start) * 1000)
    return out


def test_always_on_guard_recall_is_not_pathological(tmp_path):
    service = build(tmp_path, 300)
    best = min(statistics.mean(timings(service, 10)) for _ in range(3))
    service.close()
    assert best < 250, f"mean recall {best:.1f} ms on 300 records"


@pytest.mark.perf
def test_recall_p95_under_50ms_on_5000_records(tmp_path):
    service = build(tmp_path, 5000)
    timings(service, 5)                                   # warm the caches
    samples = sorted(timings(service, 100))
    service.close()
    assert samples[94] < 50, f"p95 {samples[94]:.1f} ms"
