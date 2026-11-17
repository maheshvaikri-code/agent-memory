import json

import pytest
from ison_graph import ISONGraph

from agent_memory.record import (
    BACKGROUND, GUARDRAIL, KINDS, MemoryRecord, build_text, envelope_props,
    make_id, normalize_text,
)


def test_build_text_uses_kind_template():
    assert build_text("bug_pattern", {"name": "webhook_storm", "signature": "unbounded retry"}) \
        == "webhook_storm: unbounded retry"
    assert build_text("alternative", {"option": "MongoDB", "rejected_because": "no transactions"}) \
        == "Rejected: MongoDB — no transactions"


def test_build_text_drops_missing_parts():
    assert build_text("decision", {"title": "Use Postgres", "context": None}) == "Use Postgres"
    assert build_text("convention", {"category": "naming", "rule": "snake_case"}) == "[naming] snake_case"


def test_make_id_is_stable_and_prefix_safe():
    a = make_id("convention", "[naming] snake_case functions everywhere")
    b = make_id("convention", "[naming] snake_case functions everywhere")
    c = make_id("convention", "[naming] snake_case functions everywhere else")
    assert a == b and a != c
    assert not any(ch in a for ch in " :|")
    assert a.startswith("convention_")


def test_from_node_fills_legacy_defaults():
    g = ISONGraph(name="t")
    g.add_node("convention", "conv_x", category="naming", rule="snake_case",
               enforced_by="ruff", exceptions="", applies_to="services/cache")
    rec = MemoryRecord.from_node(g.get_node("convention", "conv_x"))
    assert rec.source == "legacy" and rec.status == "active"
    assert rec.confidence == 0.6
    assert rec.scope == ["*services/cache*"]
    assert rec.priority == "should"
    assert rec.text == "[naming] snake_case"


def test_envelope_round_trips_through_a_file(tmp_path):
    g = ISONGraph(name="t")
    env = envelope_props(agent="codex", session_id="s1", source="explicit", evidence="PR 12",
                         commit="abc", scope=["src/**", "lib/*.py"], priority="must", confidence=0.9)
    g.add_node("decision", "d1", title="Use Postgres", context="transactions", **env)
    # a second node of the same type without the envelope: its columns load as None
    g.add_node("decision", "d2", title="Old one", context="")
    path = tmp_path / "m.memory"
    g.save(str(path))
    h = ISONGraph.load(str(path))
    new = MemoryRecord.from_node(h.get_node("decision", "d1"))
    old = MemoryRecord.from_node(h.get_node("decision", "d2"))
    assert (new.agent, new.scope, new.priority, new.confidence) == ("codex", ["src/**", "lib/*.py"], "must", 0.9)
    assert (old.agent, old.scope, old.priority, old.source) == ("unknown", ["*"], "should", "legacy")


def test_decision_status_aliases():
    g = ISONGraph(name="t")
    g.add_node("decision", "a", title="A", status="accepted")
    g.add_node("decision", "b", title="B", status="superseded")
    g.add_node("decision", "c", title="C", status="deprecated")
    get = lambda i: MemoryRecord.from_node(g.get_node("decision", i)).status
    assert (get("a"), get("b"), get("c")) == ("active", "superseded", "archived")


def test_malformed_envelope_falls_back_and_warns():
    g = ISONGraph(name="t")
    g.add_node("decision", "d", title="T", scope="[not json", confidence="high", helpful="x")
    warnings = []
    rec = MemoryRecord.from_node(g.get_node("decision", "d"), warnings)
    assert rec.scope == ["*"] and rec.confidence == 0.6 and rec.helpful == 0
    assert warnings and all("d" in w for w in warnings)


def test_tiers_and_registry_shape():
    assert KINDS["bug_pattern"].tier == GUARDRAIL
    assert KINDS["open_question"].tier == BACKGROUND
    assert "session" not in KINDS and "task" not in KINDS
    assert normalize_text("  Use   Postgres ") == "use postgres"
