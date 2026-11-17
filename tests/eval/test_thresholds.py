"""The retrieval-level gate from the spec, section 9.

The wall-clock bounds of 50 ms are marked `perf` and run only with `pytest -m perf`:
on a machine that throttles its CPU they fail on unchanged code. The default suite
keeps a loose guard (`test_recall_latency_guard`) that still catches a complexity
regression such as the pairwise de-duplication that once took 5.9 s per recall.
"""
import statistics
import time

import pytest

from agent_memory import AgentMemory

from .harness import evaluate
from .run import build, make_fns
from .scenarios import SCENARIOS

TOPICS = ["webhook", "cache", "token", "migration", "indexer", "refund", "ledger", "queue"]


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


@pytest.mark.perf
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


# -- helpers for the guard and the other perf paths ---------------------------------

def _five_thousand(tmp_path, conventions: int = 0) -> AgentMemory:
    m = AgentMemory("big", repo_root=str(tmp_path))
    for n in range(5000):
        topic = TOPICS[n % len(TOPICS)]
        m.graph.add_node("quirk", f"q{n}", dependency_id="d",
                         description=f"{topic} component {n} misbehaves when input {n * 7} is large",
                         scope=f'["services/{topic}/**"]')
    for n in range(conventions):
        topic = TOPICS[n % len(TOPICS)]
        m.graph.add_node("convention", f"c{n}", category="c",
                         rule=f"{topic} component misbehaves on large input {n} use guard")
    m.recall(task="warm up", files=["services/cache/lru.py"])
    m.check("warm up")
    return m


def _p95(call, rounds: int, before=lambda n: None) -> float:
    samples = []
    for n in range(rounds):
        before(n)
        start = time.perf_counter()
        call(n)
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.quantiles(samples, n=20)[18]


def _recall(m: AgentMemory):
    def call(n: int) -> None:
        topic = TOPICS[n % len(TOPICS)]
        m.recall(task=f"{topic} misbehaves on large input", files=[f"services/{topic}/main.py"])
    return call


def test_recall_latency_guard(tmp_path):
    """Always on: best of three p95s under 250 ms at 5,000 records. Loose enough for a
    throttled CPU (~3x slower), tight enough to catch an O(n^2) step (5,863 ms)."""
    m = _five_thousand(tmp_path)
    best = min(_p95(_recall(m), 20) for _ in range(3))
    assert best < 250, f"best-of-3 p95 {best:.1f} ms"


@pytest.mark.perf
def test_p95_latency_after_a_write_before_each_recall(tmp_path):
    m = _five_thousand(tmp_path)
    p95 = _p95(_recall(m), 40,
               before=lambda n: m.graph.update_node("quirk", f"q{n}", confidence=0.5 + n / 100))
    assert p95 < 50, f"p95 {p95:.1f} ms"


@pytest.mark.perf
def test_p95_latency_alternating_recall_and_check(tmp_path):
    m = _five_thousand(tmp_path, conventions=400)
    recall = _recall(m)

    def alternate(n: int) -> None:      # recall, check, recall, check, ...
        if n % 2:
            m.check(f"{TOPICS[n // 2 % len(TOPICS)]} misbehaves on large input")
        else:
            recall(n // 2)

    p95 = _p95(alternate, 80)
    assert p95 < 50, f"p95 {p95:.1f} ms over 40 recalls and 40 checks"
