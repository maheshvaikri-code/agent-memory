from types import SimpleNamespace

from agent_memory import AgentMemory
from agent_memory.record import GUARDRAIL, GUIDANCE, KINDS, MemoryRecord
from agent_memory.retrieval.budget import TokenCounter
from agent_memory.retrieval.channels import graph_neighbours, proximity, scope_scores
from agent_memory.retrieval.render import render, render_item


def records(m):
    return {n.ref: MemoryRecord.from_node(n) for k in KINDS for n in m.graph.nodes(k)}


def test_proximity_tiers():
    assert proximity("a/b/c.py", "a/b/c.py") == 1.0
    assert proximity("a/b/c.py", "a/b/d.py") == 0.6
    assert proximity("a/b/x/c.py", "a/b/y/d.py") == 0.3
    assert proximity("a/x/c.py", "a/y/d.py") == 0.0
    assert proximity("c.py", "d.py") == 0.6


def test_scope_scores_from_globs_and_anchors(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "a.py").write_text("x\n", encoding="utf-8")
    m = AgentMemory("t", repo_root=str(tmp_path))
    glob = m.remember("convention", {"category": "c", "rule": "scoped rule"}, scope=["svc/**"])
    anchored = m.remember("bug_pattern", {"name": "p", "signature": "s"}, anchors=["svc/a.py"])
    everywhere = m.remember("convention", {"category": "c", "rule": "global rule"})
    scores, promoted = scope_scores(m.graph, records(m), ["svc/b.py"])
    assert scores[glob.ref] == 1.0
    assert scores[anchored.ref] == 0.6
    assert everywhere.ref not in scores and promoted == {}


def test_decision_constraining_the_module_is_promoted():
    m = AgentMemory("t")
    m.codebase.add_module("src/api", "HTTP layer")
    m.decisions.add_decision("Use async", "concurrency")
    m.decisions.add_constraint("Use async", "src/api", "all handlers must be async")
    scores, promoted = scope_scores(m.graph, records(m), ["src/api/orders.py"])
    ref = next(r for r in promoted)
    assert ref[0] == "decision" and scores[ref] == 1.0
    assert promoted[ref] == "all handlers must be async"


def test_graph_neighbours_follow_whitelisted_edges_both_ways():
    m = AgentMemory("t")
    m.bugs.add_pattern("race", "shared state without a lock")
    m.bugs.add_fix_pattern("race", "use asyncio.Lock", "")
    recs = records(m)
    pattern = next(r for r in recs if r[0] == "bug_pattern")
    fix = next(r for r in recs if r[0] == "fix_pattern")
    out = graph_neighbours(m.graph, {pattern: 0.02}, set(recs))
    assert out[fix] == (0.01, pattern)
    assert graph_neighbours(m.graph, {fix: 0.02}, set(recs))[pattern][0] == 0.01


def test_heuristic_counter_is_ceil_of_chars_over_four():
    c = TokenCounter.heuristic()
    assert (c.count(""), c.count("abcd"), c.count("abcde")) == (0, 1, 2)


def item(id_, text, tier, staleness="fresh", why=""):
    return SimpleNamespace(record=SimpleNamespace(id=id_), text=text, tier=tier,
                           staleness=staleness, why=why)


def test_render_groups_by_tier_and_flags_stale_items():
    out = render([
        item("c1", "[naming] snake_case", GUIDANCE, why="matched: naming"),
        item("b1", "storm: unbounded retry", GUARDRAIL, staleness="drifted"),
    ])
    lines = out.splitlines()
    assert lines[0] == "## Guardrails"
    assert lines[1] == "- [b1] storm: unbounded retry"
    assert lines[2].startswith("  ⚠")
    assert "## Guidance" in lines and "  why: matched: naming" in lines
    assert "## Background" not in out


def test_render_compact_and_empty():
    assert render([]) == ""
    assert "why:" not in render_item(item("c1", "x", GUIDANCE, why="matched: x"), compact=True)


def _constrained_memory():
    m = AgentMemory("t")
    m.codebase.add_module("src/api", "HTTP layer")
    m.decisions.add_decision("Use async", "concurrency")
    m.decisions.add_constraint("Use async", "src/api", "all handlers must be async")
    return m


def test_promotion_respects_path_boundaries():
    m = _constrained_memory()
    scores, promoted = scope_scores(m.graph, records(m), ["src/api_old/x.py"])
    decision = ("decision", "dec_use_async")
    assert promoted == {} and decision not in scores


def test_promotion_accepts_windows_style_query_paths():
    m = _constrained_memory()
    scores, promoted = scope_scores(m.graph, records(m), ["src\\api\\orders.py"])
    assert len(promoted) == 1 and list(scores.values()) == [1.0]


def test_decision_without_constraint_is_not_promoted():
    m = AgentMemory("t")
    m.codebase.add_module("src/api", "HTTP layer")
    m.decisions.add_decision("Use async", "concurrency")
    scores, promoted = scope_scores(m.graph, records(m), ["src/api/orders.py"])
    assert promoted == {}
