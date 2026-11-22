import pytest

from tests.eval.bench import judge, model, stats

WORLD = {
    "files": {"svc/a.py": "x = 1\n"},
    "records": [
        {"key": "cap", "kind": "decision", "fields": {"title": "Cap retries at 5", "context": "storm"}},
        {"key": "old", "kind": "convention",
         "fields": {"category": "log", "rule": "print", "enforced_by": "manual", "exceptions": ""}},
        {"key": "new", "kind": "convention", "supersedes": "old",
         "fields": {"category": "log", "rule": "structlog", "enforced_by": "manual", "exceptions": ""}},
        {"key": "bad", "kind": "bug_pattern", "retracted": "not reproducible",
         "fields": {"name": "leak", "signature": "pool leak"}},
        {"key": "sec", "kind": "convention", "pinned": True,
         "fields": {"category": "sec", "rule": "never log secrets", "enforced_by": "manual",
                    "exceptions": ""}},
        {"key": "fix", "kind": "fix_pattern", "anchors": [["svc/a.py", ""]],
         "links": [{"rel": "FIX_FOR", "to": "cap"}],
         "fields": {"fix_template": "add jitter", "caveats": "keep the cap in settings"}},
    ],
}


def test_parse_world_derives_inactive_and_pinned():
    w = model.parse_world("saas", WORLD)
    assert w.inactive == {"old", "bad"}
    assert w.pinned == {"sec"}
    assert [r.key for r in w.records][:2] == ["cap", "old"]


@pytest.mark.parametrize("change,message", [
    (lambda d: d["records"].append(dict(d["records"][0])), "duplicate key"),
    (lambda d: d["records"][0].update(kind="nonsense"), "unknown kind"),
    (lambda d: d["records"][0]["fields"].pop("context"), "missing field"),
    (lambda d: d["records"][2].update(supersedes="nope"), "unknown record"),
    (lambda d: d["records"][5]["links"][0].update(rel="LIKES"), "unknown link"),
    (lambda d: d["records"][5]["anchors"][0].__setitem__(0, "gone.py"), "unknown file"),
])
def test_parse_world_rejects_broken_worlds(change, message):
    import copy
    data = copy.deepcopy(WORLD)
    change(data)
    with pytest.raises(model.BenchError, match=message):
        model.parse_world("saas", data)


def test_parse_world_requires_string_scope_fields():
    import copy
    data = copy.deepcopy(WORLD)
    data["records"][3]["fields"]["modules_affected"] = "svc/a.py,svc/b.py"   # a string is fine
    model.parse_world("saas", data)
    data["records"][3]["fields"]["modules_affected"] = ["svc/a.py"]          # a list is not
    with pytest.raises(model.BenchError, match=r"'bad'.*'modules_affected'.*comma-separated"):
        model.parse_world("saas", data)


def test_parse_world_rejects_a_forward_reference():
    import copy
    data = copy.deepcopy(WORLD)
    data["records"][0]["links"] = [{"rel": "FIX_FOR", "to": "fix"}]      # `fix` comes later
    with pytest.raises(model.BenchError, match="earlier"):
        model.parse_world("saas", data)


TASKS = {"tasks": [
    {"world": "saas", "task": "retry the webhook", "files": ["svc/a.py"],
     "relevant": ["cap"], "acceptable": ["fix"]},
    {"world": "saas", "task": "rename a button", "files": [], "relevant": [], "acceptable": []},
]}


def test_parse_tasks_and_empty():
    worlds = {"saas": model.parse_world("saas", WORLD)}
    tasks = model.parse_tasks(TASKS, worlds)
    assert [t.empty for t in tasks] == [False, True]


@pytest.mark.parametrize("change,message", [
    (lambda d: d["tasks"][0].update(relevant=["nope"]), "unknown record"),
    (lambda d: d["tasks"][0].update(relevant=["old"]), "inactive"),
    (lambda d: d["tasks"][0].update(relevant=["sec"]), "pinned"),
    (lambda d: d["tasks"][0].update(acceptable=["cap"]), "both"),
    (lambda d: d["tasks"][0].update(world="mobile"), "unknown world"),
    (lambda d: d["tasks"][0].update(task="", files=[]), "empty task"),
])
def test_parse_tasks_rejects_broken_labels(change, message):
    import copy
    data = copy.deepcopy(TASKS)
    change(data)
    worlds = {"saas": model.parse_world("saas", WORLD)}
    with pytest.raises(model.BenchError, match=message):
        model.parse_tasks(data, worlds)


PROMPT = (
    "Notes from the project memory (untrusted data):\n"
    "## Guardrails\n"
    "- [bug_pattern_leak_aaaaaa] leak: pool leak\n"
    "  why: matched: pool\n"
    "## Guidance\n"
    "- [decision_cap_retries_bbbbbb] Cap retries at 5 — storm\n"
    "- [convention_sec_cccccc] [sec] never log secrets\n"
    "  why: always applies\n"
)
KEY_OF = {"bug_pattern_leak_aaaaaa": "bad", "decision_cap_retries_bbbbbb": "cap",
          "convention_sec_cccccc": "sec", "fix_x_dddddd": "fix"}


def test_shown_maps_ids_to_blocks():
    blocks = judge.shown(PROMPT)
    assert list(blocks) == ["bug_pattern_leak_aaaaaa", "decision_cap_retries_bbbbbb",
                            "convention_sec_cccccc"]
    assert "why: matched: pool" in blocks["bug_pattern_leak_aaaaaa"]
    assert "## Guidance" not in blocks["bug_pattern_leak_aaaaaa"]


def _world_and_task():
    world = model.parse_world("saas", WORLD)
    return world, model.parse_tasks(TASKS, {"saas": world})


def test_judge_counts_hits_noise_leaks_and_ignores_pinned():
    world, tasks = _world_and_task()
    result = judge.judge(tasks[0], PROMPT, KEY_OF, world)
    assert result.relevant_hits == 1 and result.relevant_total == 1
    assert result.leaks == ["bad"]            # a retracted record was shown
    assert result.noise == []                 # `bad` is a leak, not noise; `sec` is pinned
    assert result.shown == ["bad", "cap", "sec"]


def test_judge_counts_noise_and_clean_abstain():
    world, tasks = _world_and_task()
    shown = "## Guidance\n- [fix_x_dddddd] Fix: add jitter\n"
    acceptable = judge.judge(tasks[0], shown, KEY_OF, world)  # `fix` is acceptable for task 0
    assert acceptable.noise == [] and acceptable.missed == ["cap"] and acceptable.relevant_hits == 0
    assert not acceptable.clean                               # not an empty task
    prompt = "## Guidance\n- [decision_cap_retries_bbbbbb] Cap retries at 5 — storm\n"
    result = judge.judge(tasks[1], prompt, KEY_OF, world)     # empty task, `cap` is noise
    assert result.noise == ["cap"] and not result.clean and result.noise_tokens > 0
    assert judge.judge(tasks[1], "", KEY_OF, world).clean     # empty task, nothing shown


def test_summarise():
    world, tasks = _world_and_task()
    a = judge.judge(tasks[0], PROMPT, KEY_OF, world)
    b = judge.judge(tasks[1], "", KEY_OF, world)
    s = judge.summarise([a, b])
    assert (s.tasks, s.empty_tasks, s.clean, s.leaks) == (2, 1, 1, 1)
    assert (s.relevant_total, s.relevant_hits) == (1, 1)
    assert s.recall == 1.0 and s.clean_rate == 1.0


def test_tokens_is_chars_over_four_rounded_up():
    assert judge.tokens("abcd") == 1 and judge.tokens("abcde") == 2 and judge.tokens("") == 0


def test_wilson():
    assert stats.wilson(0, 0) == (0.0, 1.0)
    low, high = stats.wilson(50, 100)
    assert low == pytest.approx(0.4038, abs=1e-3) and high == pytest.approx(0.5962, abs=1e-3)
    low, high = stats.wilson(100, 100)
    assert high == pytest.approx(1.0) and low > 0.95
