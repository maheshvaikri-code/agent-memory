from agent_memory import AgentMemory
from agent_memory.context import CodingContext


def build():
    m = AgentMemory("t")
    m.conventions.add_convention("naming", "cache keys use colon namespaces", "ruff", None, "src/cache")
    m.conventions.add_idiom("null_check", "if x is None", "if not x")
    m.decisions.add_decision("Use Postgres for billing", "transactions")
    m.decisions.add_decision("Cache entries expire after an hour", "bounded staleness")
    m.bugs.add_pattern("cache_stampede", "concurrent cache misses recompute the key",
                       modules_affected=["src/cache"])
    m.bugs.add_fix_pattern("cache_stampede", "take a per-key lock", "")
    m.codebase.add_module("src/cache", "LRU caching layer")
    m.performance.add_hot_path("cache_lookup", "very_high", untouchable=True)
    return m


def test_context_is_task_specific_now():
    ctx = build().get_context("fix the cache stampede", files=["src/cache/lru.py"])
    assert [b["pattern"]["name"] for b in ctx.bug_risks if "pattern" in b] == ["cache_stampede"]
    assert any("fix" in b for b in ctx.bug_risks)      # the fix arrives by graph hop
    assert any(c.get("rule") == "cache keys use colon namespaces" for c in ctx.conventions)
    titles = [d.get("title") for d in ctx.decisions]
    assert "Cache entries expire after an hour" in titles
    assert "Use Postgres for billing" not in titles
    assert ctx.estimated_tokens > 0 and ctx.items


def test_unrelated_task_gets_an_empty_context():
    ctx = build().get_context("fix CSS on the landing page", files=["web/landing.css"])
    assert ctx.items == [] and ctx.to_prompt() == ""
    assert ctx.conventions == [] and ctx.decisions == []


def test_prompt_contains_every_selected_item():
    ctx = build().get_context("fix the cache stampede lookup", files=["src/cache/lru.py"])
    prompt = ctx.to_prompt()
    assert all(f"[{i.record.id}]" in prompt for i in ctx.items)
    assert "Cache entries expire after an hour" in prompt


def test_to_dict_keeps_its_keys():
    data = build().get_context("fix the cache stampede").to_dict()
    assert {"task", "files", "warnings", "constraints", "hot_paths", "conventions", "patterns",
            "bug_risks", "dependencies", "module_context", "decisions", "team_experts",
            "session_context", "estimated_tokens"} <= set(data)


def test_include_and_exclude_filter_by_the_old_store_names():
    m = build()
    only = m.get_context("fix the cache stampede", files=["src/cache/lru.py"], include=["bug_risks"])
    assert only.bug_risks and not only.conventions and not only.decisions
    without = m.get_context("fix the cache stampede", files=["src/cache/lru.py"], exclude=["bug_risks"])
    assert not without.bug_risks and without.conventions


def test_hand_built_context_still_renders_the_old_way():
    ctx = CodingContext(task="t", conventions=[{"category": "naming", "rule": "snake_case"}])
    assert "## CODE CONVENTIONS" in ctx.to_prompt()


def test_quick_context_returns_a_string():
    assert isinstance(build().get_quick_context("fix the cache stampede"), str)
