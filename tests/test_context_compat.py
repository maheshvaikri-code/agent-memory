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


# -- filtered contexts keep the always-on sets -----------------------------------------

def build_with_rule():
    m = AgentMemory("t")
    m.remember("convention", {"category": "security", "rule": "never log secrets or tokens",
                              "applies_to": "*"}, priority="must")
    m.remember("bug_pattern", {"name": "webhook_storm", "signature": "unbounded retry on webhook"})
    return m


def test_excluding_an_unrelated_category_keeps_the_pinned_rule():
    m = build_with_rule()
    full = m.get_context("add retry to payment webhook")
    excl = m.get_context("add retry to payment webhook", exclude=["team_experts"])
    assert "secrets" in full.to_prompt()
    assert "secrets" in excl.to_prompt()
    assert any(c.get("rule") == "never log secrets or tokens" for c in excl.conventions)


def test_include_naming_none_of_the_pinned_buckets_drops_the_pinned_rule():
    ctx = build_with_rule().get_context("add retry to payment webhook", include=["bug_risks"])
    assert "secrets" not in ctx.to_prompt() and ctx.bug_risks


def test_include_naming_the_pinned_rules_bucket_keeps_it():
    ctx = build_with_rule().get_context("add retry to payment webhook",
                                        include=["bug_risks", "conventions"])
    assert "secrets" in ctx.to_prompt()


def test_excluding_an_unrelated_category_keeps_carried_session_items():
    m = build_with_rule()
    sid = m.start_session("refund worker")
    m.sessions.add_open_question(sid, "max batch size for the refund worker?")
    ctx = m.get_context("add a dark mode toggle", exclude=["team_experts"])
    assert any("max batch size" in q["question"] for q in ctx.session_context["open_questions"])
    only = m.get_context("add a dark mode toggle", include=["bug_risks"])
    assert "max batch size" not in only.to_prompt()


# -- return shapes of spec section 8 ---------------------------------------------------

def test_every_bug_risk_entry_has_a_pattern_and_a_fix_key():
    ctx = build().get_context("fix the cache stampede", files=["src/cache/lru.py"])
    assert ctx.bug_risks
    for b in ctx.bug_risks:
        assert b["type"] == "bug_risk" and set(b) >= {"pattern", "fix"}
        assert b["pattern"]["name"]                       # the old access path works
    (stampede,) = [b for b in ctx.bug_risks if b["pattern"]["name"] == "cache_stampede"]
    assert stampede["fix"]["fix_template"] == "take a per-key lock"


def test_a_pattern_without_a_fix_has_fix_none():
    m = AgentMemory("t")
    m.bugs.add_pattern("webhook_storm", "unbounded retry on webhook")
    (entry,) = m.get_context("unbounded retry on the webhook").bug_risks
    assert entry["pattern"]["name"] == "webhook_storm" and entry["fix"] is None


def test_a_fix_whose_pattern_did_not_rank_is_not_its_own_bug_risk_entry():
    m = AgentMemory("t")
    m.bugs.add_pattern("cache_stampede", "concurrent misses recompute the key")
    m.bugs.add_fix_pattern("cache_stampede", "take a per-key lock with a timeout", "")
    ctx = m.get_context("per-key lock timeout", include=["bug_risks"])
    assert any(i.record.kind == "fix_pattern" for i in ctx.items)
    assert all("pattern" in b for b in ctx.bug_risks)
    assert "per-key lock" in ctx.to_prompt()


def test_session_context_has_all_its_keys_whenever_a_session_exists():
    m = build()
    assert m.get_context("fix the cache stampede").session_context == {}
    sid = m.start_session("cache work")
    m.sessions.add_open_question(sid, "should the stampede lock be per key?")
    ctx = m.get_context("paint the logo purple")
    sc = ctx.session_context
    assert {"session", "previous_sessions", "decisions", "partial_work",
            "open_questions", "blockers"} <= set(sc)
    assert sc["session"]["task"] == "cache work"
    assert sc["decisions"] == [] and sc["blockers"] == [] and sc["partial_work"] == []
    assert [q["question"] for q in sc["open_questions"]] == ["should the stampede lock be per key?"]


def test_previous_sessions_follow_the_continues_chain():
    m = build()
    first = m.start_session("one")
    m.end_session(first)
    second = m.start_session("two")
    m.sessions.continue_session(second, first)
    sc = m.get_context("anything").session_context
    assert sc["session"]["task"] == "two"
    assert [s["task"] for s in sc["previous_sessions"]] == ["one"]
