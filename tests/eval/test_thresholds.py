"""The retrieval-level gate from the spec, section 9."""
import statistics
import time

import pytest

from agent_memory import AgentMemory

from .harness import evaluate
from .run import build, make_fns
from .scenarios import SCENARIOS


@pytest.fixture(scope="module")
def card(tmp_path_factory):
    memory = build(tmp_path_factory.mktemp("shopline"))
    return evaluate(*make_fns(memory))


def test_scenario_count_and_balance():
    by = {}
    for s in SCENARIOS:
        by[s.condition] = by.get(s.condition, 0) + 1
    assert len(SCENARIOS) >= 48 and len(by) == 8 and min(by.values()) >= 6


def test_must_include_recall(card):
    assert card.recall >= 0.90, card.failures


def test_no_superseded_or_retracted_leaks(card):
    assert card.leaks == 0, card.failures


def test_abstention(card):
    assert card.abstention >= 0.90, card.failures


def test_every_condition_passes_most_of_its_scenarios(card):
    weak = {c: f"{sum(r)}/{len(r)}" for c, r in card.by_condition.items() if sum(r) < len(r) - 1}
    assert not weak, (weak, card.failures)


def test_p95_latency_at_five_thousand_records(tmp_path):
    m = AgentMemory("big", repo_root=str(tmp_path))
    topics = ["webhook", "cache", "token", "migration", "indexer", "refund", "ledger", "queue"]
    for n in range(5000):
        topic = topics[n % len(topics)]
        m.graph.add_node("quirk", f"q{n}", dependency_id="d",
                         description=f"{topic} component {n} misbehaves when input {n * 7} is large",
                         scope=f'["services/{topic}/**"]')
    m.recall(task="warm up", files=["services/cache/lru.py"])
    samples = []
    for n in range(40):
        topic = topics[n % len(topics)]
        start = time.perf_counter()
        m.recall(task=f"{topic} misbehaves on large input", files=[f"services/{topic}/main.py"])
        samples.append((time.perf_counter() - start) * 1000)
    p95 = statistics.quantiles(samples, n=20)[18]
    assert p95 < 50, f"p95 {p95:.1f} ms"
