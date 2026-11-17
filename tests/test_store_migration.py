from agent_memory import AgentMemory
from agent_memory.record import KINDS, MemoryRecord


def populate(m):
    m.codebase.add_module("src/cache", "LRU caching layer")
    m.codebase.add_pattern("singleton", ["src/cache"], why_chosen="one instance")
    m.conventions.add_convention("naming", "snake_case", "ruff")
    m.conventions.add_idiom("null_check", "if x is None", "if not x")
    m.conventions.add_project_pattern("error_handling", "raise CustomError(code=)", "always")
    m.decisions.add_decision("Use async", "High concurrency expected")
    m.decisions.add_alternative("Use async", "threading", "GIL limitations")
    m.bugs.add_bug("Race condition in cache", severity="critical")
    m.bugs.add_pattern("race_condition", "shared_state_no_lock")
    m.bugs.add_fix_pattern("race_condition", "use asyncio.Lock", "check reentrant need")
    m.dependencies.add_dependency("rocksdb", "0.22.0", "storage")
    m.dependencies.add_quirk("rocksdb", "batch > 4MB causes stall", severity="high")
    m.dependencies.add_workaround("batch > 4MB causes stall", "chunk into 2MB batches")
    m.tests.add_convention("unit", "test_{module}_{function}_{case}")
    m.tests.add_required_cases("graph_operation", ["empty", "cyclic"])
    m.performance.add_hot_path("node_lookup", "very_high", untouchable=True)
    m.performance.add_constraint("node_lookup", "<5ms p99", "user experience")
    m.techdebt.add_debt("src/traverse.py", "god function", severity="high")
    m.techdebt.add_refactoring_attempt("src/traverse.py", "split by type", "failed", "circular imports")
    m.team.add_expertise("graph_algorithms", "deep", ["src/traverse"])
    m.templates.add_template("api_endpoint", "handler")
    m.templates.add_snippet("async_lock", "async with lock:", "shared state access")
    sid = m.start_session("Add batch API")
    m.sessions.add_decision(sid, "use streaming", "memory efficiency", sticky=True)
    m.sessions.add_open_question(sid, "max batch size?")
    m.sessions.add_partial_work(sid, "function", "src/batch.py", "half written")
    m.sessions.add_blocker(sid, "waiting on schema review")


def test_every_registry_node_written_by_a_store_has_an_envelope():
    m = AgentMemory("t", agent="claude-code")
    populate(m)
    seen = set()
    for kind in KINDS:
        for node in m.graph.nodes(kind):
            rec = MemoryRecord.from_node(node)
            seen.add(kind)
            assert rec.source == "explicit", f"{kind}:{node.id} was not written through the writer"
            assert rec.agent == "claude-code" and rec.created_at
            assert rec.text, f"{kind}:{node.id} renders to empty text"
    assert seen == set(KINDS), f"kinds never created: {set(KINDS) - seen}"


def test_adding_the_same_convention_twice_no_longer_raises():
    m = AgentMemory("t")
    first = m.conventions.add_convention("naming", "snake_case", "ruff")
    second = m.conventions.add_convention("naming", "snake_case", "ruff")
    assert first == second
    assert len(list(m.graph.nodes("convention"))) == 1


def test_rules_sharing_a_twenty_character_prefix_both_survive():
    m = AgentMemory("t")
    a = m.conventions.add_convention("naming", "functions use snake_case names always", "ruff")
    b = m.conventions.add_convention("naming", "functions use snake_case names in tests too", "ruff")
    assert a != b
    assert len(list(m.graph.nodes("convention"))) == 2


def test_fix_pattern_for_an_existing_fix_text_does_not_raise():
    m = AgentMemory("t")
    m.bugs.add_pattern("race_a", "shared state without a lock")
    m.bugs.add_pattern("race_b", "shared counter without a lock")
    m.bugs.add_fix_pattern("race_a", "use asyncio.Lock", "")
    m.bugs.add_fix_pattern("race_b", "use asyncio.Lock", "")
    m.bugs.add_fix_pattern("race_a", "use asyncio.Lock", "")


def test_store_ids_are_valid_graph_identifiers():
    m = AgentMemory("t")
    ids = [
        m.decisions.add_decision("Architecture: layered", "ctx"),
        m.decisions.add_decision("Pick A | B", "ctx"),
        m.conventions.add_convention("style: x", "use a|b everywhere", "ruff"),
        m.bugs.add_pattern("race: a|b", "shared state"),
    ]
    for node_id in ids:
        assert node_id and not any(ch in node_id for ch in " :|"), node_id


def test_make_id_keeps_already_valid_ids_unchanged():
    m = AgentMemory("t")
    name = "naming snake_case/a.b-c"
    old = f"conv_{name.replace(' ', '_').replace('/', '_').replace('.', '_').lower()}"
    assert m.conventions._make_id("conv", name) == old
