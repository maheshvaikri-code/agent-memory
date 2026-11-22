# Recall Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a benchmark that cannot be tuned against by accident, measure the current retriever on it, then make `recall()` stay quiet on tasks the memory knows nothing about without losing a real match.

**Architecture:** A benchmark package `tests/eval/bench/` (three hand-written worlds as JSON, graded task labels in a dev and a locked test split, a loader, a judge, a runner). Retrieval changes live in the relevance floor of `src/agent_memory/retrieval/retriever.py`, with the tier logic in a new small module `retrieval/evidence.py`. One lever per commit, each measured on the dev split.

**Tech Stack:** Python 3.9+ core (no new dependencies), pytest, the existing `agent_memory` engine and `MemoryService`.

**Spec:** `docs/superpowers/specs/2026-11-21-recall-quality-design.md`

## Global Constraints

- Core stays Python 3.9+ and dependency-free. `from __future__ import annotations` is allowed in `retrieval/*` (already used there); do not use 3.10-only syntax (`X | Y` types, `match`).
- Line endings: new files are LF. When editing an existing file, preserve its line endings (most working-tree source files are CRLF; use the Edit tool, never a script that rewrites whole files).
- Commit author is already configured. **No `Co-Authored-By` or any attribution line on any commit.** Never push, never touch remotes or git config. One lever per commit.
- Run tests as `python -m pytest tests -q -p no:cacheprovider`. Run the eval as `PYTHONPATH=src python -m tests.eval.run --service`. `test_recall_latency_guard` can flake alone on a throttled laptop CPU: re-run it alone before calling it a regression.
- `amem` on PATH may belong to a different checkout: use `python -m agent_memory.cli ...` from this repo.
- **Isolation of the benchmark.** The authors of worlds and tasks (Tasks 3-11) must not read anything under `src/agent_memory/retrieval/`, must not call `recall()` or `check()`, and must not look at retrieval results. Only the validation tests (which never call recall) and `src/agent_memory/record.py` are theirs to read.
- **Locked test split.** `tests/eval/bench/tasks/test-*.json` are committed in Tasks 9-11, before any retrieval change, and never edited afterwards. A wrong label found later goes in `tests/eval/bench/RESULTS.md` under "Label corrections", with the corrected figure beside the original.
- Spec acceptance rules (section 6), fixed in advance, measured on the test split: (1) must-include recall does not fall (a fall of up to 1 point is accepted only if noise tokens fall 70% or more); (2) superseded and retracted leaks stay 0; (3) mean noise tokens per task fall by at least 50%; (4) clean-abstain rate on empty tasks at least 0.80; (5) `test_recall_latency_guard` still passes and median recall at 5,000 records is reported; (6) no existing scenario, expectation or held-out label changes; (7) the full default suite passes.
- No existing file under `tests/eval/` (`scenarios.py`, `world.py`, `heldout.py`, `harness.py`, `run.py`) changes.

## Review Focus

Inputs and conditions the spec implies but no obvious task exercises, most likely to bite first. Each has a test in the task named.

1. A task whose text is all generic words ("process it") but names a file in scope of a relevant record: the record must still appear, via scope (Task 14).
2. The pinned record (`priority: must`, scope `*`) is returned with and without a query match, and the tiers never drop it (Task 14).
3. Results must not depend on the order records were written (Task 14).
4. A store below `SMALL_STORE` records keeps weak matches: a new user's first notes are not dropped for being "weak" (Task 15).
5. A camelCase word counts as one concept: "MongoDB" (`mongo`, `db`, `mongodb`) is not two (Task 13).

---

### Task 1: Benchmark core (data model, judge, statistics, authoring guide)

**Files:**
- Create: `tests/eval/bench/__init__.py` (empty)
- Create: `tests/eval/bench/model.py`
- Create: `tests/eval/bench/judge.py`
- Create: `tests/eval/bench/stats.py`
- Create: `tests/eval/bench/AUTHORING.md`
- Create: `tests/eval/test_bench_core.py`

**Interfaces:**
- Produces (used by every later task):
  - `model.BenchError(ValueError)`; `model.WORLDS = ("saas", "pipeline", "mobile")`; `model.SPLITS = ("dev", "test")`; `model.WORLD_DIR`, `model.TASK_DIR` (`Path`s).
  - `model.WorldRecord` (frozen dataclass): `key: str, kind: str, fields: dict, scope: Optional[list] = None, pinned: bool = False, anchors: list = [], supersedes: str = "", retracted: str = "", links: list = []`. `anchors` is `[[path, symbol], ...]`; `links` is `[{"rel": "FIX_FOR", "to": "<key>"}, ...]`.
  - `model.World`: `name: str, files: dict, records: list` with properties `inactive` (set of keys superseded or retracted) and `pinned` (set of pinned keys), and `by_key()`.
  - `model.Task` (frozen dataclass): `world: str, task: str, files: list, relevant: list, acceptable: list`; property `empty` (no relevant keys).
  - `model.parse_world(name, data) -> World` (validates), `model.load_world(name) -> World`, `model.parse_tasks(data, worlds) -> list[Task]`, `model.load_tasks(split) -> list[Task]` (reads every `tasks/<split>-<world>.json` that exists, in `WORLDS` order).
  - `judge.shown(prompt: str) -> dict[str, str]` (record id -> its rendered block), `judge.TaskResult`, `judge.judge(task: Task, prompt: str, key_of: dict) -> TaskResult`, `judge.Summary`, `judge.summarise(results) -> Summary`, `judge.tokens(text) -> int`.
  - `stats.wilson(hits, total, z=1.96) -> (low, high)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_bench_core.py`:

```python
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
    assert high == 1.0 and low > 0.95
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/eval/test_bench_core.py -q -p no:cacheprovider`
Expected: collection error, `No module named 'tests.eval.bench'`.

- [ ] **Step 3: Write `tests/eval/bench/__init__.py` (empty) and `stats.py`**

`tests/eval/bench/stats.py`:

```python
"""Small statistics for the benchmark report."""
import math
from typing import Tuple


def wilson(hits: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """95% Wilson score interval for a proportion. (0, 1) when there is nothing to count."""
    if total <= 0:
        return 0.0, 1.0
    p = hits / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)
```

- [ ] **Step 4: Write `model.py`**

```python
"""Benchmark data: worlds, labelled tasks, and the checks that keep them honest."""
import json
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from agent_memory.record import KINDS
from agent_memory.retrieval.channels import GRAPH_EDGES

BENCH = Path(__file__).parent
WORLD_DIR = BENCH / "worlds"
TASK_DIR = BENCH / "tasks"
WORLDS = ("saas", "pipeline", "mobile")
SPLITS = ("dev", "test")


class BenchError(ValueError):
    """A world or task file breaks a rule of the benchmark."""


@dataclass(frozen=True)
class WorldRecord:
    key: str
    kind: str
    fields: Dict[str, object]
    scope: Optional[List[str]] = None
    pinned: bool = False
    anchors: List[List[str]] = field(default_factory=list)
    supersedes: str = ""
    retracted: str = ""
    links: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class World:
    name: str
    files: Dict[str, str]
    records: List[WorldRecord]

    def by_key(self) -> Dict[str, WorldRecord]:
        return {r.key: r for r in self.records}

    @property
    def inactive(self) -> Set[str]:
        out = {r.supersedes for r in self.records if r.supersedes}
        return out | {r.key for r in self.records if r.retracted}

    @property
    def pinned(self) -> Set[str]:
        return {r.key for r in self.records if r.pinned}


@dataclass(frozen=True)
class Task:
    world: str
    task: str
    files: List[str]
    relevant: List[str]
    acceptable: List[str]

    @property
    def empty(self) -> bool:
        return not self.relevant


def _template_fields(kind: str) -> List[str]:
    return [name for _, name, _, _ in string.Formatter().parse(KINDS[kind].template) if name]


def parse_world(name: str, data: dict) -> World:
    files = dict(data.get("files") or {})
    seen: Dict[str, int] = {}
    records: List[WorldRecord] = []
    for index, raw in enumerate(data.get("records") or []):
        key = raw.get("key")
        where = f"world {name!r} record #{index} ({key!r})"
        if not key or not isinstance(key, str):
            raise BenchError(f"{where}: a record needs a key")
        if key in seen:
            raise BenchError(f"{where}: duplicate key")
        kind = raw.get("kind")
        if kind not in KINDS:
            raise BenchError(f"{where}: unknown kind {kind!r}")
        fields = dict(raw.get("fields") or {})
        for needed in _template_fields(kind):
            if not str(fields.get(needed, "")).strip():
                raise BenchError(f"{where}: missing field {needed!r} of kind {kind!r}")
        rec = WorldRecord(key=key, kind=kind, fields=fields, scope=raw.get("scope"),
                          pinned=bool(raw.get("pinned")),
                          anchors=[list(a) for a in raw.get("anchors") or []],
                          supersedes=raw.get("supersedes", ""), retracted=raw.get("retracted", ""),
                          links=[dict(link) for link in raw.get("links") or []])
        if rec.supersedes and rec.supersedes not in seen:
            raise BenchError(f"{where}: supersedes an unknown record or one that is not earlier "
                             f"({rec.supersedes!r})")
        for link in rec.links:
            if link.get("rel") not in GRAPH_EDGES:
                raise BenchError(f"{where}: unknown link {link.get('rel')!r}; use one of {GRAPH_EDGES}")
            if link.get("to") not in seen:
                raise BenchError(f"{where}: link target {link.get('to')!r} must be an earlier record")
        for path, *_ in rec.anchors:
            if path not in files:
                raise BenchError(f"{where}: anchor on an unknown file {path!r}")
        seen[key] = index
        records.append(rec)
    return World(name, files, records)


def load_world(name: str) -> World:
    path = WORLD_DIR / f"{name}.json"
    return parse_world(name, json.loads(path.read_text(encoding="utf-8")))


def parse_tasks(data: dict, worlds: Dict[str, World]) -> List[Task]:
    out: List[Task] = []
    for index, raw in enumerate(data.get("tasks") or []):
        where = f"task #{index} ({raw.get('task')!r})"
        world = worlds.get(raw.get("world"))
        if world is None:
            raise BenchError(f"{where}: unknown world {raw.get('world')!r}")
        task = Task(world=world.name, task=str(raw.get("task") or ""),
                    files=list(raw.get("files") or []), relevant=list(raw.get("relevant") or []),
                    acceptable=list(raw.get("acceptable") or []))
        if not task.task.strip() and not task.files:
            raise BenchError(f"{where}: empty task (no text and no files)")
        known = world.by_key()
        for label, keys in (("relevant", task.relevant), ("acceptable", task.acceptable)):
            for key in keys:
                if key not in known:
                    raise BenchError(f"{where}: {label} names an unknown record {key!r}")
                if key in world.inactive:
                    raise BenchError(f"{where}: {label} names an inactive record {key!r}")
                if key in world.pinned:
                    raise BenchError(f"{where}: {label} names a pinned record {key!r}; "
                                     "pinned records are always allowed")
        both = set(task.relevant) & set(task.acceptable)
        if both:
            raise BenchError(f"{where}: {sorted(both)} are both relevant and acceptable")
        out.append(task)
    return out


def load_tasks(split: str) -> List[Task]:
    if split not in SPLITS:
        raise BenchError(f"unknown split {split!r}")
    worlds = {w: load_world(w) for w in WORLDS if (WORLD_DIR / f"{w}.json").exists()}
    tasks: List[Task] = []
    for name in WORLDS:
        path = TASK_DIR / f"{split}-{name}.json"
        if path.exists():
            tasks.extend(parse_tasks(json.loads(path.read_text(encoding="utf-8")), worlds))
    return tasks
```

- [ ] **Step 5: Write `judge.py`**

```python
"""Scores one task from the prompt a recall rendered. Reads only the text, like an agent would."""
from dataclasses import dataclass, field
from typing import Dict, Iterable, List

from .model import Task, World


def tokens(text: str) -> int:
    """The default token count the retriever budgets with: chars / 4, rounded up."""
    return -(-len(text) // 4)


def shown(prompt: str) -> Dict[str, str]:
    """Record id -> the rendered block (its line plus indented lines after it), in order."""
    out: Dict[str, List[str]] = {}
    current = None
    for line in prompt.splitlines():
        if line.startswith("- [") and "]" in line:
            current = line[3:line.index("]")]
            out[current] = [line]
        elif current is not None and line.startswith("  "):
            out[current].append(line)
        else:
            current = None
    return {rid: "\n".join(lines) for rid, lines in out.items()}


@dataclass
class TaskResult:
    world: str
    task: str
    empty: bool
    shown: List[str]
    relevant_total: int
    relevant_hits: int
    missed: List[str] = field(default_factory=list)
    noise: List[str] = field(default_factory=list)
    noise_tokens: int = 0
    leaks: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """An empty task that showed no noise and no leak."""
        return self.empty and not self.noise and not self.leaks


def judge(task: Task, prompt: str, key_of: Dict[str, str], world: World) -> TaskResult:
    blocks = shown(prompt)
    keys = [(key_of[rid], rid) for rid in blocks if rid in key_of]
    allowed = set(task.relevant) | set(task.acceptable) | world.pinned
    result = TaskResult(task.world, task.task, task.empty, [k for k, _ in keys],
                        len(task.relevant), 0)
    present = {k for k, _ in keys}
    result.relevant_hits = sum(1 for k in task.relevant if k in present)
    result.missed = [k for k in task.relevant if k not in present]
    for key, rid in keys:
        if key in world.inactive:
            result.leaks.append(key)
        elif key not in allowed:
            result.noise.append(key)
            result.noise_tokens += tokens(blocks[rid] + "\n")
    return result


@dataclass
class Summary:
    tasks: int = 0
    empty_tasks: int = 0
    relevant_total: int = 0
    relevant_hits: int = 0
    noise_records: int = 0
    noise_tokens: int = 0
    clean: int = 0
    leaks: int = 0

    @property
    def recall(self) -> float:
        return self.relevant_hits / self.relevant_total if self.relevant_total else 1.0

    @property
    def clean_rate(self) -> float:
        return self.clean / self.empty_tasks if self.empty_tasks else 1.0

    @property
    def noise_tokens_per_task(self) -> float:
        return self.noise_tokens / self.tasks if self.tasks else 0.0

    @property
    def noise_per_task(self) -> float:
        return self.noise_records / self.tasks if self.tasks else 0.0


def summarise(results: Iterable[TaskResult]) -> Summary:
    s = Summary()
    for r in results:
        s.tasks += 1
        s.empty_tasks += r.empty
        s.relevant_total += r.relevant_total
        s.relevant_hits += r.relevant_hits
        s.noise_records += len(r.noise)
        s.noise_tokens += r.noise_tokens
        s.clean += r.clean
        s.leaks += len(r.leaks)
    return s
```

- [ ] **Step 6: Write `AUTHORING.md`** (full text, verbatim)

````markdown
# Authoring the recall benchmark

You are writing data, not code. Read this file and `KINDS` in `src/agent_memory/record.py`
(only that dict). **Do not read `src/agent_memory/retrieval/`, do not call `recall()` or
`check()`, and do not look at how any retriever scores your work.** The point of the benchmark
is that its authors did not tune it to a retriever. Your only check is the validation test
named in your task.

## 1. A world

A world is the memory store of one imaginary project, plus its file tree. It lives in
`tests/eval/bench/worlds/<name>.json`:

```json
{
  "files": {"app/webhooks/handlers.py": "def handle(event):\n    ...\n"},
  "records": [
    {"key": "webhook-retry-cap", "kind": "decision",
     "fields": {"title": "Cap webhook retries at 5", "context": "INC-212: an unbounded retry storm took the queue down"},
     "anchors": [["app/webhooks/handlers.py", "handle"]]},
    {"key": "never-log-secrets", "kind": "convention", "pinned": true,
     "fields": {"category": "security", "rule": "never log secrets or tokens",
                "enforced_by": "manual", "exceptions": ""}},
    {"key": "retry-storm", "kind": "bug_pattern",
     "fields": {"name": "retry_storm", "signature": "unbounded retry of a failing webhook floods the provider"}},
    {"key": "retry-fix", "kind": "fix_pattern",
     "fields": {"fix_template": "cap attempts and add jitter", "caveats": "keep the cap in settings"},
     "links": [{"rel": "FIX_FOR", "to": "retry-storm"}]},
    {"key": "old-log-rule", "kind": "convention",
     "fields": {"category": "logging", "rule": "log with print", "enforced_by": "manual", "exceptions": ""}},
    {"key": "log-rule", "kind": "convention", "supersedes": "old-log-rule",
     "fields": {"category": "logging", "rule": "use structlog with a request id", "enforced_by": "manual", "exceptions": ""}},
    {"key": "pool-leak", "kind": "bug_pattern", "retracted": "could not reproduce",
     "fields": {"name": "pool_leak", "signature": "connections leak when the pool raises"}}
  ]
}
```

Record fields:

- `key`: short unique name you invent. Tasks label records by key. Records are written in file
  order; `supersedes` and `links[].to` must name an **earlier** record.
- `kind` and `fields`: every `{placeholder}` in the kind's template in `KINDS` must be a
  non-empty field. The template is how the record reads in a prompt, so write fields that read
  like a note a developer's agent would really have saved: a reason, an incident, a number.
- `scope`: optional list of file globs (`["app/webhooks/**"]`). Some kinds derive scope from a
  field (see `scope_field` in `KINDS`); you can also set it directly.
- `anchors`: `[[path, symbol]]`, paths from `files`. Ties the note to a file.
- `pinned`: true for the one or two always-on rules of the project (`priority: must`, scope `*`).
- `supersedes`: this record replaces an earlier one; the earlier one stops being returned.
- `retracted`: a reason; the record was found wrong and stops being returned.
- `links`: `{"rel": one of FIX_FOR, WORKAROUND_FOR, HAS_QUIRK, CONSIDERED_FOR, ATTEMPTED_ON,
  INSTANCE_OF, TEST_FOR_BUG, "to": earlier key}`. This is how a bug pattern brings its fix along.

Write 80 to 100 records per world (superseded and retracted ones count), using at least twelve
different kinds, with the project's real vocabulary. Do not write near-duplicate notes: the
engine merges them and the loader rejects a merge. Two records of a "natural key" kind (a
`decision` title, a `module` path, a `hot_path` name, and so on) must not share the key unless
one supersedes the other.

**Shared vocabulary on purpose.** Real projects reuse ordinary words in unrelated places. Use
these naturally, where they fit, in records of different areas: process, handle, document,
runner, export, import, set, write, error, test, config, log, schema, update, migrate, queue.
Do not force them, and do not make a record about the word; a decision about "document
storage" and a bug in "the export job" and a convention for "error responses" are the shape.

## 2. The file tree

`files` holds 12 to 25 small realistic source or config files (a few lines each). Anchors and
task file lists draw from it.

## 3. A task

Tasks live in `tests/eval/bench/tasks/<split>-<world>.json`:

```json
{"tasks": [
  {"world": "saas", "task": "add retry with backoff to the webhook sender",
   "files": ["app/webhooks/handlers.py"],
   "relevant": ["webhook-retry-cap", "retry-storm"], "acceptable": ["retry-fix"]}
]}
```

Write the task the way a developer would give it to a coding agent: a plain sentence or two,
sometimes naming files, sometimes only files, sometimes only text. Mix the shapes. Roughly 60%
should list files.

Label every record you judge to matter:

- **relevant**: a competent engineer doing this task would want to be told this before
  starting. Showing it is required.
- **acceptable**: related to the task, harmless or mildly useful to see, not required.
- **everything else is noise**, including records that share a word with the task but mean
  something else.

An **empty task** has `"relevant": []`: nothing stored needs to be shown (it may still have
acceptable records). About a third of your tasks must be empty: plausible work in some other
area of the project, or work the memory simply knows nothing about. Make them realistic, not
absurd. Include tasks whose words collide with records of another area ("document the release
process" when a record mentions "documents" in a search index).

Give relevant records only where you are sure. Never label a superseded, retracted or pinned
record. Never label to make anything easy or hard for a retriever; you cannot see one.

## 4. Checking your work

The validation test named in your task parses everything and checks counts. Fix what it
reports. Do not weaken a test. When it passes, you are done.
````

- [ ] **Step 7: Run the tests**

Run: `python -m pytest tests/eval/test_bench_core.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add tests/eval/bench tests/eval/test_bench_core.py
git commit -m "test: benchmark core (model, judge, stats, authoring guide)"
```

---

### Task 2: World builder

**Files:**
- Create: `tests/eval/bench/build.py`
- Create: `tests/eval/test_bench_build.py`

**Interfaces:**
- Consumes: `model.World`, `model.WorldRecord`, `model.BenchError`, `judge.shown`, `judge.judge`.
- Produces: `build.Built` (dataclass: `world: World, memory: AgentMemory, service: MemoryService, ids: dict[key, id], key_of: dict[id, key]`) and `build.build_world(world, root: Path) -> Built`. `root` is a directory the file tree is written under; the memory's `repo_root` is `root`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_bench_build.py`:

```python
import pytest

from tests.eval.bench import judge, model
from tests.eval.bench.build import build_world

from .test_bench_core import WORLD


def _built(tmp_path):
    return build_world(model.parse_world("saas", WORLD), tmp_path)


def test_every_record_gets_an_id_and_the_map_inverts(tmp_path):
    built = _built(tmp_path)
    assert set(built.ids) == {"cap", "old", "new", "bad", "sec", "fix"}
    assert len(set(built.ids.values())) == 6
    assert {built.key_of[i]: i for i in built.key_of} == built.ids


def test_files_are_written(tmp_path):
    _built(tmp_path)
    assert (tmp_path / "svc" / "a.py").read_text() == "x = 1\n"


def test_service_prompt_shows_active_and_pinned_records_and_never_inactive_ones(tmp_path):
    built = _built(tmp_path)
    prompt = built.service.recall("cap retries storm structlog pool leak", [], max_tokens=2000)["prompt"]
    keys = {built.key_of[i] for i in judge.shown(prompt) if i in built.key_of}
    assert "cap" in keys and "sec" in keys          # relevant record and the pinned rule
    assert "new" in keys                            # the superseding rule is active
    assert not keys & built.world.inactive          # old (superseded) and bad (retracted)


def test_a_link_brings_the_fix_along(tmp_path):
    built = _built(tmp_path)
    prompt = built.service.recall("cap retries storm", [], max_tokens=2000)["prompt"]
    assert built.ids["fix"] in judge.shown(prompt)


def test_a_record_the_engine_merges_is_an_error(tmp_path):
    data = {"files": {}, "records": [
        {"key": "a", "kind": "convention",
         "fields": {"category": "x", "rule": "use the query builder", "enforced_by": "m", "exceptions": ""}},
        {"key": "b", "kind": "convention",
         "fields": {"category": "x", "rule": "use the query builder", "enforced_by": "m", "exceptions": ""}},
    ]}
    with pytest.raises(model.BenchError, match="'b'"):
        build_world(model.parse_world("saas", data), tmp_path)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/eval/test_bench_build.py -q -p no:cacheprovider`
Expected: `No module named 'tests.eval.bench.build'`.

- [ ] **Step 3: Write `build.py`**

```python
"""Builds a benchmark world into a real store, the way an agent would have filled it."""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from agent_memory import AgentMemory
from agent_memory.service import MemoryService

from .model import BenchError, World


@dataclass
class Built:
    world: World
    memory: AgentMemory
    service: MemoryService
    ids: Dict[str, str]
    key_of: Dict[str, str]


def build_world(world: World, root: Path) -> Built:
    root = Path(root)
    for rel, content in world.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    memory = AgentMemory(world.name, repo_root=str(root))
    ids: Dict[str, str] = {}
    refs = {}
    for rec in world.records:
        kw = {}
        if rec.scope is not None:
            kw["scope"] = list(rec.scope)
        if rec.pinned:
            kw.update(priority="must", scope=["*"])
        if rec.supersedes:
            result = memory.supersede(ids[rec.supersedes], rec.kind, dict(rec.fields), **kw)
        else:
            result = memory.remember(rec.kind, dict(rec.fields), **kw)
        if not result.created or result.merged:
            raise BenchError(f"world {world.name!r}: the engine merged record {rec.key!r} into "
                             "another record; make its text distinct")
        ids[rec.key] = result.id
        refs[rec.key] = result.ref
        for path, symbol in ((a[0], a[1] if len(a) > 1 else "") for a in rec.anchors):
            memory.anchor(result.id, path, symbol)
        for link in rec.links:
            memory.graph.add_edge(link["rel"], result.ref, refs[link["to"]])
        if rec.retracted:
            memory.retract(result.id, rec.retracted)
    return Built(world, memory, MemoryService(memory=memory), ids, {v: k for k, v in ids.items()})
```

- [ ] **Step 4: Run the tests; fix what they find**

Run: `python -m pytest tests/eval/test_bench_build.py tests/eval/test_bench_core.py -q -p no:cacheprovider`
Expected: pass. If `test_a_link_brings_the_fix_along` fails because the fix is not returned, check that the link was created in the right direction before changing anything else (the graph channel reads either direction). If `supersede` on a kind with a natural key behaves differently from the test world's `convention`, the test world is the authority; report rather than change the retriever. If `anchor` rejects an empty symbol, pass the symbol only when non-empty.

- [ ] **Step 5: Commit**

```bash
git add tests/eval/bench/build.py tests/eval/test_bench_build.py
git commit -m "test: build benchmark worlds into a real store"
```

---

### Task 3: World "saas" (Taskboard)

**Files:**
- Create: `tests/eval/bench/worlds/saas.json`
- Create: `tests/eval/test_bench_data.py`

**Interfaces:**
- Consumes: `model.load_world`, `model.parse_world`, `build.build_world`, `AUTHORING.md`.
- Produces: `worlds/saas.json`, and the validation test file that Tasks 4 and 5 extend.

Read first: `tests/eval/bench/AUTHORING.md` (your requirements) and `KINDS` in `src/agent_memory/record.py`. Do not read anything under `src/agent_memory/retrieval/`; do not call recall.

**The world:** *Taskboard*, a B2B project-management web app: Django-style Python backend, React frontend, Postgres, Redis cache and queue, Stripe-like billing provider, S3-like file storage, email and Slack notifications, a public REST API with webhooks, admin tools, reports and CSV export, onboarding wizard, search, SSO. About 90 records. It must not copy the existing `tests/eval/world.py` ("shopline": payments webhooks, redis pipeline, JWT expiry, migrations runner, indexer) beyond what any web app shares.

- [ ] **Step 1: Write the validation test**

Create `tests/eval/test_bench_data.py`:

```python
"""Structure checks for the benchmark's worlds and tasks. None of these call recall."""
import hashlib
import re

import pytest

from agent_memory.record import KINDS
from agent_memory.retrieval.tokenize import tokenize

from tests.eval.bench import model
from tests.eval.bench.build import build_world

PRESENT = [w for w in model.WORLDS if (model.WORLD_DIR / f"{w}.json").exists()]
GENERIC = ["process", "handle", "document", "runner", "export", "import", "set", "write",
           "error", "test", "config", "log", "schema", "update", "migrate", "queue"]


@pytest.fixture(scope="module")
def worlds():
    return {name: model.load_world(name) for name in PRESENT}


@pytest.mark.parametrize("name", PRESENT)
def test_world_size_and_shape(worlds, name):
    w = worlds[name]
    kinds = {r.kind for r in w.records}
    assert 80 <= len(w.records) <= 110, len(w.records)
    assert len(kinds) >= 12, sorted(kinds)
    assert 12 <= len(w.files) <= 25, len(w.files)
    assert len(w.inactive) >= 6, "needs at least six superseded or retracted records"
    assert sum(1 for r in w.records if r.links) >= 8
    assert 1 <= len(w.pinned) <= 2
    assert sum(1 for r in w.records if r.anchors) >= 15
    scoped = sum(1 for r in w.records if r.anchors or r.scope or KINDS[r.kind].scope_field)
    assert scoped >= 0.4 * len(w.records), scoped


@pytest.mark.parametrize("name", PRESENT)
def test_world_builds_without_merges(worlds, name, tmp_path):
    built = build_world(worlds[name], tmp_path)
    assert len(built.ids) == len(worlds[name].records)
    built.memory.close() if hasattr(built.memory, "close") else None


@pytest.mark.parametrize("name", PRESENT)
def test_world_texts_are_not_near_duplicates(worlds, name):
    seen = {}
    for r in worlds[name].records:
        text = " ".join(str(v) for v in r.fields.values()).lower()
        assert text not in seen, f"{r.key} repeats {seen[text]}"
        seen[text] = r.key
```

- [ ] **Step 2: Run it to see it skip or pass trivially, then write the world**

Run: `python -m pytest tests/eval/test_bench_data.py -q -p no:cacheprovider` (no world yet: no parameters, passes vacuously).

Write `tests/eval/bench/worlds/saas.json` following `AUTHORING.md`. Write it in several passes if it is long; run the test after the first 40 records to catch field mistakes early.

- [ ] **Step 3: Run the validation until it passes**

Run: `python -m pytest tests/eval/test_bench_data.py -q -p no:cacheprovider`
Expected: 3 passed (one per test, parametrized over `saas`).

- [ ] **Step 4: Commit**

```bash
git add tests/eval/bench/worlds/saas.json tests/eval/test_bench_data.py
git commit -m "test: benchmark world saas (Taskboard)"
```

---

### Task 4: World "pipeline" (Ledgerflow)

**Files:**
- Create: `tests/eval/bench/worlds/pipeline.json`

**Interfaces:**
- Consumes: `AUTHORING.md`, `test_bench_data.py` (unchanged).

Read first: `tests/eval/bench/AUTHORING.md` and `KINDS` in `src/agent_memory/record.py`. Do not read `src/agent_memory/retrieval/` and do not call recall. You may skim `worlds/saas.json` for format only; do not reuse its content.

**The world:** *Ledgerflow*, a nightly data platform: Airflow-style DAGs, Spark and SQL transforms, a columnar warehouse, ingestion from partner SFTP drops and REST APIs, schema registry and data contracts, data-quality checks, backfills, cost controls, a metrics API, a notebook runner, CSV and Parquet exports, PII handling. About 90 records. Not a web app: its notes are about jobs, partitions, schemas, backfills, retries, idempotency, lineage and cost.

- [ ] **Step 1: Write the world** per `AUTHORING.md`, targeting 85 to 95 records.

- [ ] **Step 2: Run the validation**

Run: `python -m pytest tests/eval/test_bench_data.py -q -p no:cacheprovider`
Expected: passes for `saas` and `pipeline`.

- [ ] **Step 3: Commit**

```bash
git add tests/eval/bench/worlds/pipeline.json
git commit -m "test: benchmark world pipeline (Ledgerflow)"
```

---

### Task 5: World "mobile" (Trailmate) and the shared-vocabulary check

**Files:**
- Create: `tests/eval/bench/worlds/mobile.json`
- Modify: `tests/eval/test_bench_data.py` (append one test)

**Interfaces:**
- Consumes: `AUTHORING.md`, `test_bench_data.py` helpers (`PRESENT`, `GENERIC`, `worlds` fixture, `tokenize`).

Read first: `tests/eval/bench/AUTHORING.md` and `KINDS` in `src/agent_memory/record.py`. Do not read `src/agent_memory/retrieval/` (the shared-vocabulary test below uses only the tokenizer) and do not call recall.

**The world:** *Trailmate*, a cross-platform mobile app (React Native) for trail maps with a companion command-line tool: offline sync, GPS and background location, push notifications, device permissions, in-app purchases, app-store release tooling, crash reporting, map-tile caching, a CLI for exporting tracks and importing GPX files. About 90 records.

- [ ] **Step 1: Append the shared-vocabulary test**

Append to `tests/eval/test_bench_data.py`:

```python
def test_generic_words_recur_across_worlds(worlds):
    """The worlds must share ordinary vocabulary, or document frequency means nothing."""
    assert set(worlds) == set(model.WORLDS), "all three worlds must exist"
    stems = {tokenize(word)[0] for word in GENERIC}
    reused = 0
    for stem in stems:
        in_worlds = 0
        for w in worlds.values():
            hits = sum(1 for r in w.records
                       if stem in tokenize(" ".join(str(v) for v in r.fields.values())))
            in_worlds += hits >= 2
        reused += in_worlds >= 2
    assert reused >= 10, f"only {reused} of {len(stems)} generic words recur in two worlds"
```

- [ ] **Step 2: Write the world** per `AUTHORING.md`, targeting 85 to 95 records.

- [ ] **Step 3: Run the validation**

Run: `python -m pytest tests/eval/test_bench_data.py -q -p no:cacheprovider`
Expected: all pass, including `test_generic_words_recur_across_worlds`. If that test fails, add ordinary uses of the missing words to records in the world that fits them (do not stuff a word in where it does not belong, and do not edit the other two worlds).

- [ ] **Step 4: Commit**

```bash
git add tests/eval/bench/worlds/mobile.json tests/eval/test_bench_data.py
git commit -m "test: benchmark world mobile (Trailmate) and shared-vocabulary check"
```

---

### Task 6: Dev tasks, saas

**Files:**
- Create: `tests/eval/bench/tasks/dev-saas.json`
- Modify: `tests/eval/test_bench_data.py` (append the task-structure tests; later tasks reuse them)

**Interfaces:**
- Consumes: `model.load_tasks`, `model.parse_tasks`, the three worlds, `AUTHORING.md` section 3.
- Produces: `dev-saas.json` (32 to 38 tasks) and the parametrized task checks.

Read first: `tests/eval/bench/AUTHORING.md`. Do not read `src/agent_memory/retrieval/`, do not call recall. Read `worlds/saas.json` fully: you label against it.

- [ ] **Step 1: Append the task-structure tests**

```python
SPLIT_FILES = sorted(p.name[:-5] for p in model.TASK_DIR.glob("*-*.json")) if model.TASK_DIR.exists() else []
TASK_SETS = [(n.split("-")[0], n.split("-")[1]) for n in SPLIT_FILES]       # (split, world)
SIZE = {"dev": (32, 38), "test": (62, 72)}


@pytest.fixture(scope="module")
def tasks():
    return {split: model.load_tasks(split) for split in model.SPLITS}


@pytest.mark.parametrize("split,name", TASK_SETS)
def test_task_set_shape(tasks, split, name):
    mine = [t for t in tasks[split] if t.world == name]
    low, high = SIZE[split]
    assert low <= len(mine) <= high, len(mine)
    empty = sum(1 for t in mine if t.empty)
    assert 0.28 <= empty / len(mine) <= 0.40, f"{empty}/{len(mine)} empty"
    for t in mine:
        if not t.empty:
            assert 1 <= len(t.relevant) <= 4, t
        assert len(t.acceptable) <= 6, t
    assert sum(1 for t in mine if t.files) >= 0.5 * len(mine)
    assert len({t.task.strip().lower() for t in mine if t.task.strip()}) == sum(
        1 for t in mine if t.task.strip()), "duplicate task text"


def test_no_task_text_is_shared_between_splits(tasks):
    dev = {t.task.strip().lower() for t in tasks["dev"] if t.task.strip()}
    test = {t.task.strip().lower() for t in tasks["test"] if t.task.strip()}
    assert not dev & test
```

- [ ] **Step 2: Write the dev tasks**

Write `tests/eval/bench/tasks/dev-saas.json` per `AUTHORING.md` section 3: 32 to 38 tasks, about a third empty (relevant `[]`), a spread of shapes (task text only, files only, both), a few word-collision tasks. Label by reading the world, never by guessing.

- [ ] **Step 3: Run the checks**

Run: `python -m pytest tests/eval/test_bench_data.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/eval/bench/tasks/dev-saas.json tests/eval/test_bench_data.py
git commit -m "test: benchmark dev tasks, saas"
```

---

### Task 7: Dev tasks, pipeline

**Files:**
- Create: `tests/eval/bench/tasks/dev-pipeline.json`

Same rules and reading list as Task 6, against `worlds/pipeline.json`: 32 to 38 tasks, about a third empty. Do not read `src/agent_memory/retrieval/`; do not call recall. The task-structure tests already exist.

- [ ] **Step 1: Write `dev-pipeline.json`** per `AUTHORING.md` section 3, labelling against `worlds/pipeline.json`.

- [ ] **Step 2: Run the checks**

Run: `python -m pytest tests/eval/test_bench_data.py -q -p no:cacheprovider`
Expected: all pass (the shape test now runs for `dev-saas` and `dev-pipeline`).

- [ ] **Step 3: Commit**

```bash
git add tests/eval/bench/tasks/dev-pipeline.json
git commit -m "test: benchmark dev tasks, pipeline"
```

---

### Task 8: Dev tasks, mobile

**Files:**
- Create: `tests/eval/bench/tasks/dev-mobile.json`

Same rules and reading list as Task 6, against `worlds/mobile.json`: 32 to 38 tasks, about a third empty. Do not read `src/agent_memory/retrieval/`; do not call recall.

- [ ] **Step 1: Write `dev-mobile.json`** per `AUTHORING.md` section 3, labelling against `worlds/mobile.json`.

- [ ] **Step 2: Run the checks**

Run: `python -m pytest tests/eval/test_bench_data.py -q -p no:cacheprovider`
Expected: all pass. Dev total is 96 to 114 tasks.

- [ ] **Step 3: Commit**

```bash
git add tests/eval/bench/tasks/dev-mobile.json
git commit -m "test: benchmark dev tasks, mobile"
```

---

### Task 9: Test tasks, saas (locked)

**Files:**
- Create: `tests/eval/bench/tasks/test-saas.json`

Read first: `tests/eval/bench/AUTHORING.md` and `worlds/saas.json`. You may skim `tasks/dev-saas.json` for style; your tasks must not repeat any of its task texts (a check enforces this). Do not read `src/agent_memory/retrieval/`; do not call recall. **This file is the locked test split: after the commit it is never edited.** Take extra care over labels: nobody can correct them later without a recorded label correction.

- [ ] **Step 1: Write `test-saas.json`**: 62 to 72 tasks, about a third empty, the spread of shapes in `AUTHORING.md`, a good share of word-collision tasks.

- [ ] **Step 2: Self-audit.** Re-read twenty of your tasks against the world and confirm each `relevant` record would genuinely be wanted before starting, and each empty task truly has nothing stored that matters. Fix errors now: this is the last time you can.

- [ ] **Step 3: Run the checks**

Run: `python -m pytest tests/eval/test_bench_data.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/eval/bench/tasks/test-saas.json
git commit -m "test: benchmark test tasks, saas (locked)"
```

---

### Task 10: Test tasks, pipeline (locked)

**Files:**
- Create: `tests/eval/bench/tasks/test-pipeline.json`

Same as Task 9, against `worlds/pipeline.json`; style reference `tasks/dev-pipeline.json`. 62 to 72 tasks. Do not read `src/agent_memory/retrieval/`; do not call recall. Locked after the commit.

- [ ] **Step 1: Write `test-pipeline.json`.**
- [ ] **Step 2: Self-audit** twenty tasks against the world; fix now.
- [ ] **Step 3: Run the checks:** `python -m pytest tests/eval/test_bench_data.py -q -p no:cacheprovider`; expected all pass.
- [ ] **Step 4: Commit**

```bash
git add tests/eval/bench/tasks/test-pipeline.json
git commit -m "test: benchmark test tasks, pipeline (locked)"
```

---

### Task 11: Test tasks, mobile (locked) and the checksum guard

**Files:**
- Create: `tests/eval/bench/tasks/test-mobile.json`
- Create: `tests/eval/bench/tasks/test.sha256`
- Modify: `tests/eval/test_bench_data.py` (append the checksum test)

Same as Task 9 for `worlds/mobile.json` (style reference `tasks/dev-mobile.json`), 62 to 72 tasks. Do not read `src/agent_memory/retrieval/`; do not call recall.

- [ ] **Step 1: Write `test-mobile.json`, self-audit twenty tasks, fix now.**

- [ ] **Step 2: Append the checksum test**

```python
def _digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_locked_test_split_is_unchanged():
    """The test split is locked. An edit shows up here and must be argued for in review;
    a wrong label is recorded in bench/RESULTS.md, not fixed in place."""
    lock = model.TASK_DIR / "test.sha256"
    assert lock.exists(), "tasks/test.sha256 is missing"
    expected = dict(line.split()[::-1] for line in lock.read_text().splitlines() if line.strip())
    files = sorted(model.TASK_DIR.glob("test-*.json"))
    assert sorted(expected) == [p.name for p in files]
    for path in files:
        assert _digest(path) == expected[path.name], f"{path.name} changed after it was locked"
```

- [ ] **Step 3: Write the lock file** after all three test files are final:

```bash
cd tests/eval/bench/tasks && python - <<'EOF'
import hashlib, pathlib
lines = []
for p in sorted(pathlib.Path(".").glob("test-*.json")):
    data = p.read_bytes().replace(b"\r\n", b"\n")
    lines.append(hashlib.sha256(data).hexdigest() + "  " + p.name)
with open("test.sha256", "w", encoding="utf-8", newline="\n") as handle:
    handle.write("\n".join(lines) + "\n")
EOF
```

- [ ] **Step 4: Run the checks**

Run: `python -m pytest tests/eval/test_bench_data.py -q -p no:cacheprovider`
Expected: all pass. Test total is 186 to 216 tasks.

- [ ] **Step 5: Commit**

```bash
git add tests/eval/bench/tasks/test-mobile.json tests/eval/bench/tasks/test.sha256 tests/eval/test_bench_data.py
git commit -m "test: benchmark test tasks, mobile (locked) and checksum guard"
```

---

### Task 12: Runner, the held-out relabel, and the baseline

**Files:**
- Create: `tests/eval/bench/run.py`
- Create: `tests/eval/bench/heldout_relabel.py`
- Create: `tests/eval/bench/RESULTS.md`
- Create: `tests/eval/bench/runs/` (saved runs)
- Create: `tests/eval/test_bench_run.py`

**Interfaces:**
- Consumes: everything in Tasks 1-11; `tests.eval.heldout.HELDOUT`; `tests.eval.world.MARKERS, PINNED`; `tests.eval.run.build`; `tests.eval.eval_service.service_fns`; `tests.eval.harness.evaluate`.
- Produces:
  - `run.run_split(split: str) -> list[TaskResult]`
  - CLI: `PYTHONPATH=src python -m tests.eval.bench.run --split dev|test [--label NAME] [--save] [--against NAME]`
    - prints a summary table (counts, rates, Wilson 95% intervals for recall and clean-abstain, noise per task and noise tokens per task);
    - `--save` writes `tests/eval/bench/runs/<split>-<label>.json` (summary and per-task shown, noise, missed);
    - `--against NAME` loads `runs/<split>-NAME.json` and prints the acceptance rules 1-4 as PASS/FAIL lines.
  - `heldout_relabel.relabelled_abstention(recall_fn) -> (hits, total)`.
  - `run.rules(before: dict, after: dict) -> list[tuple[str, bool, str]]` (pure; the four rules).

**Pre-registered, do not edit after this commit:** the generic-word list for Task 17 is in that task and is committed with this plan, before the first test-split run in this task.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval/test_bench_run.py`:

```python
from tests.eval.bench import run
from tests.eval.bench.judge import Summary


def summary(**kw):
    base = dict(tasks=100, empty_tasks=30, relevant_total=200, relevant_hits=190, noise_records=100,
                noise_tokens=4000, clean=10, leaks=0)
    base.update(kw)
    return base


def test_rules_pass_when_noise_halves_and_recall_holds():
    before = summary()
    after = summary(noise_tokens=1900, clean=26)
    by_name = {name: ok for name, ok, _ in run.rules(before, after)}
    assert all(by_name.values()), by_name


def test_rule_one_allows_a_one_point_fall_only_with_a_seventy_percent_noise_cut():
    before = summary()
    small_cut = summary(relevant_hits=188, noise_tokens=1900, clean=26)     # -1.0 pt, 52% cut
    big_cut = summary(relevant_hits=188, noise_tokens=1000, clean=26)       # -1.0 pt, 75% cut
    assert not dict((n, ok) for n, ok, _ in run.rules(before, small_cut))["recall"]
    assert dict((n, ok) for n, ok, _ in run.rules(before, big_cut))["recall"]
    too_far = summary(relevant_hits=186, noise_tokens=500, clean=26)        # -2 pt
    assert not dict((n, ok) for n, ok, _ in run.rules(before, too_far))["recall"]


def test_rules_fail_on_leaks_weak_noise_cut_and_low_clean_rate():
    before = summary()
    after = summary(leaks=1, noise_tokens=3000, clean=20)
    by_name = {name: ok for name, ok, _ in run.rules(before, after)}
    assert not by_name["leaks"] and not by_name["noise"] and not by_name["clean"]


def test_a_zero_noise_baseline_cannot_pass_the_noise_rule_by_division():
    by_name = {n: ok for n, ok, _ in run.rules(summary(noise_tokens=0), summary(noise_tokens=0))}
    assert by_name["noise"]


def test_the_dev_split_runs_end_to_end_and_reports_no_leaks(tmp_path):
    results = run.run_split("dev")
    assert results and sum(len(r.leaks) for r in results) == 0
    assert all(r.world for r in results)


def test_relabelled_heldout_counts_the_three_reviewed_tasks():
    from tests.eval.bench.heldout_relabel import ACCEPTABLE
    assert set(ACCEPTABLE) == {
        "refactor the logging setup for the CLI",
        "improve error messages in the onboarding wizard",
        "configure the sentry error reporting DSN",
    }
```

- [ ] **Step 2: Run to verify failure**, then write `heldout_relabel.py`:

```python
"""The held-out set relabelled after review, printed beside the original figure.

Relabelling after seeing results is a form of tuning, so the original 0.38 stays the headline.
This marks, for three tasks, a returned record as acceptable: the review of 2026-11-21 found
the logging convention a correct match for "refactor the logging setup", and the ApiError
convention arguably useful for the two error-message tasks. Nothing else is relabelled.
"""
from typing import Callable, List, Tuple

from ..heldout import HELDOUT
from ..world import MARKERS, PINNED

ACCEPTABLE = {
    "refactor the logging setup for the CLI": {"structlog"},
    "improve error messages in the onboarding wizard": {"apierror"},
    "configure the sentry error reporting DSN": {"apierror"},
}


def relabelled_abstention(recall_fn: Callable[[str, List[str]], str]) -> Tuple[int, int]:
    hits = 0
    for s in HELDOUT:
        text = recall_fn(s.task, s.files)
        allowed = PINNED | ACCEPTABLE.get(s.task, set())
        present = [n for n, marker in MARKERS.items() if marker in text and n not in allowed]
        hits += not present
    return hits, len(HELDOUT)
```

- [ ] **Step 3: Write `run.py`**

```python
"""Run the recall benchmark: PYTHONPATH=src python -m tests.eval.bench.run --split dev|test

  --label NAME     name for the saved run (default "run")
  --save           write tests/eval/bench/runs/<split>-<label>.json
  --against NAME   compare with runs/<split>-NAME.json and print acceptance rules 1-4
"""
import dataclasses
import json
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

from . import judge, model
from .build import build_world
from .stats import wilson

RUNS = model.BENCH / "runs"


def run_split(split: str) -> List[judge.TaskResult]:
    tasks = model.load_tasks(split)
    results: List[judge.TaskResult] = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in model.WORLDS:
            mine = [t for t in tasks if t.world == name]
            if not mine:
                continue
            built = build_world(model.load_world(name), Path(tmp) / name)
            for t in mine:
                prompt = built.service.recall(t.task, t.files, max_tokens=2000)["prompt"]
                results.append(judge.judge(t, prompt, built.key_of, built.world))
            getattr(built.memory, "close", lambda: None)()
    return results


def rules(before: Dict, after: Dict) -> List[Tuple[str, bool, str]]:
    """Acceptance rules 1-4 of the spec, from two saved summaries."""
    def recall(s):
        return s["relevant_hits"] / s["relevant_total"] if s["relevant_total"] else 1.0

    def clean(s):
        return s["clean"] / s["empty_tasks"] if s["empty_tasks"] else 1.0

    cut = 1.0 if not before["noise_tokens"] else 1 - after["noise_tokens"] / before["noise_tokens"]
    fall = recall(before) - recall(after)
    recall_ok = fall <= 1e-9 or (fall <= 0.01 + 1e-9 and cut >= 0.70)
    return [
        ("recall", recall_ok, f"{recall(before):.4f} -> {recall(after):.4f} (noise cut {cut:.0%})"),
        ("leaks", after["leaks"] == 0, f"{after['leaks']} leaks"),
        ("noise", cut >= 0.50, f"noise tokens {before['noise_tokens']} -> {after['noise_tokens']} ({cut:.0%} cut)"),
        ("clean", clean(after) >= 0.80, f"clean-abstain {clean(after):.2f}"),
    ]


def _table(summary: judge.Summary) -> str:
    rlo, rhi = wilson(summary.relevant_hits, summary.relevant_total)
    clo, chi = wilson(summary.clean, summary.empty_tasks)
    return "\n".join([
        f"tasks {summary.tasks} (empty {summary.empty_tasks})",
        f"must-include recall   {summary.recall:.4f}  ({summary.relevant_hits}/{summary.relevant_total})"
        f"  95% CI {rlo:.3f}-{rhi:.3f}",
        f"clean-abstain rate    {summary.clean_rate:.4f}  ({summary.clean}/{summary.empty_tasks})"
        f"  95% CI {clo:.3f}-{chi:.3f}",
        f"noise per task        {summary.noise_per_task:.3f} records, "
        f"{summary.noise_tokens_per_task:.1f} tokens",
        f"superseded/retracted leaks  {summary.leaks}",
    ])


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)

    def option(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    split = option("--split")
    if split not in model.SPLITS:
        print(__doc__)
        return 2
    label = option("--label", "run")
    results = run_split(split)
    summary = judge.summarise(results)
    out = [f"split {split}, label {label}", _table(summary)]
    if "--save" in argv:
        RUNS.mkdir(exist_ok=True)
        path = RUNS / f"{split}-{label}.json"
        payload = {"label": label, "split": split, "summary": dataclasses.asdict(summary),
                   "tasks": [dict(world=r.world, task=r.task, empty=r.empty, shown=r.shown,
                                  missed=r.missed, noise=r.noise, noise_tokens=r.noise_tokens,
                                  leaks=r.leaks) for r in results]}
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=1) + "\n")
        out.append(f"saved {path}")
    against = option("--against")
    if against:
        before = json.loads((RUNS / f"{split}-{against}.json").read_text(encoding="utf-8"))["summary"]
        out.append(f"rules against {against}:")
        for name, ok, detail in rules(before, dataclasses.asdict(summary)):
            out.append(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the unit tests**

Run: `python -m pytest tests/eval/test_bench_run.py -q -p no:cacheprovider`
Expected: pass. (`test_the_dev_split_runs_end_to_end...` builds the three worlds and recalls ~100 tasks; it should take well under a minute. If a world fails to build, the world file is wrong: report it rather than editing the retriever.)

- [ ] **Step 5: Record the baseline on the unchanged retriever**

Run, in this order, on a clean tree with no retrieval changes:

```bash
PYTHONPATH=src python -m tests.eval.bench.run --split dev  --label baseline --save
PYTHONPATH=src python -m tests.eval.bench.run --split test --label baseline --save
PYTHONPATH=src python -m tests.eval.run --service
```

Record the baseline median recall latency at 5,000 records, on the unchanged retriever (Task 20 repeats it; the laptop throttles, so run it right after a rest and say so):

```bash
python -c "
import statistics, tempfile, time
from pathlib import Path
from tests.eval.test_thresholds import _five_thousand, _recall
with tempfile.TemporaryDirectory() as t:
    m = _five_thousand(Path(t)); call = _recall(m); s = []
    for n in range(40):
        a = time.perf_counter(); call(n); s.append((time.perf_counter() - a) * 1000)
    print('median ms', round(statistics.median(s), 1))"
```

Also compute the relabelled held-out figure once and keep the number:

```bash
PYTHONPATH=src python -c "
import tempfile; from pathlib import Path
from tests.eval.run import build
from tests.eval.eval_service import service_fns
from tests.eval.bench.heldout_relabel import relabelled_abstention
from agent_memory.service import MemoryService
with tempfile.TemporaryDirectory() as t:
    m = build(Path(t)); fn = service_fns(MemoryService(memory=m))[0]
    print(relabelled_abstention(fn))"
```

- [ ] **Step 6: Write `tests/eval/bench/RESULTS.md`** with: the baseline tables for dev and test (the runner output, verbatim), the original held-out figure (0.38, 6/16) and the relabelled figure (as computed above, marked "relabelled after review, not substituted"), a "Label corrections" section (empty: "none"), and a "Lever trials (dev split)" table whose first row is `baseline`.

- [ ] **Step 7: Run the whole default suite**

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add tests/eval/bench tests/eval/test_bench_run.py
git commit -m "test: benchmark runner, held-out relabel and baseline runs"
```

---

### Task 13: Evidence tiers (pure function, not yet wired)

**Files:**
- Create: `src/agent_memory/retrieval/evidence.py`
- Create: `tests/test_evidence.py`

**Interfaces:**
- Produces (Tasks 14-18 consume):
  - `evidence.STRONG, MEDIUM, WEAK` (strings `"strong"`, `"medium"`, `"weak"`).
  - `evidence.GENERIC_WORDS: frozenset[str]` (starts empty; Task 17 fills it) and `evidence.GENERIC_STEMS` (its stemmed form, a `frozenset`).
  - `evidence.RARE_SHARE = 0.05`, `evidence.SMALL_STORE = 0` (Task 15 sets 25).
  - `evidence.classify(*, in_scope, semantic, concepts, rarest_df, n, distinctive_df, rare_share=None) -> str`.
  - `evidence.stems(words) -> frozenset[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_evidence.py`:

```python
from agent_memory.retrieval import evidence
from agent_memory.retrieval.evidence import MEDIUM, STRONG, WEAK, classify
from agent_memory.retrieval.retriever import _camel_groups, _concepts
from agent_memory.retrieval.tokenize import compound_tokens, tokenize


def c(**kw):
    base = dict(in_scope=False, semantic=False, concepts=1, rarest_df=1, n=100, distinctive_df=2)
    base.update(kw)
    return classify(**base)


def test_a_scope_or_semantic_hit_is_strong():
    assert c(in_scope=True, concepts=0, rarest_df=None) == STRONG
    assert c(semantic=True, concepts=0, rarest_df=None) == STRONG


def test_two_informative_concepts_are_strong():
    assert c(concepts=2, rarest_df=40) == STRONG


def test_one_rare_concept_is_medium_and_one_common_concept_is_weak():
    assert c(rarest_df=5, n=100) == MEDIUM          # limit max(2, 0.05 * 100) = 5
    assert c(rarest_df=6, n=100) == WEAK


def test_no_informative_concept_is_weak():
    assert c(concepts=0, rarest_df=None) == WEAK


def test_a_small_corpus_falls_back_to_the_absolute_rule():
    assert c(rarest_df=2, n=10) == MEDIUM           # limit max(2, 0.5) = 2
    assert c(rarest_df=3, n=10) == WEAK


def test_the_share_can_be_passed_for_tuning():
    assert c(rarest_df=8, n=100, rare_share=0.10) == MEDIUM
    assert c(rarest_df=8, n=100, rare_share=0.05) == WEAK


def test_the_module_share_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(evidence, "RARE_SHARE", 0.10)
    assert c(rarest_df=8, n=100) == MEDIUM


def test_a_camelcase_word_is_one_concept():
    text = "MongoDB"
    matched = frozenset(tokenize(text) + list(compound_tokens(text)))
    assert len(matched) >= 2                          # mongo, db, mongodb
    assert _concepts(matched, _camel_groups(text)) == 1


def test_stems_uses_the_retrieval_tokenizer():
    assert evidence.stems(["write", "handle", "setting"]) == frozenset({"writ", "handl", "set"})
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_evidence.py -q -p no:cacheprovider`
Expected: `No module named 'agent_memory.retrieval.evidence'`.

- [ ] **Step 3: Write `evidence.py`**

```python
"""Evidence tiers for the recall floor: how strongly a record's match supports showing it.

A record that shares one common word with a task is usually noise ("write a script" matching
a note about writing a CSV). The floor keeps strong and medium matches and drops weak ones.
"""
from typing import FrozenSet, Iterable, Optional

from .tokenize import tokenize

STRONG, MEDIUM, WEAK = "strong", "medium", "weak"


def stems(words: Iterable[str]) -> FrozenSet[str]:
    """The tokens the retriever's tokenizer makes of `words`."""
    out = set()
    for word in words:
        out.update(tokenize(word))
    return frozenset(out)


# Generic task words: they open or fill a coding task and say nothing about which record is
# relevant. Filled in a later step; empty means the tiers rest on document frequency alone.
GENERIC_WORDS: FrozenSet[str] = frozenset()
GENERIC_STEMS: FrozenSet[str] = stems(GENERIC_WORDS)

RARE_SHARE = 0.05   # a single matched concept is rare if it is in at most this share of records
SMALL_STORE = 0     # below this many records the weak tier is not dropped


def classify(*, in_scope: bool, semantic: bool, concepts: int, rarest_df: Optional[int], n: int,
             distinctive_df: int, rare_share: Optional[float] = None) -> str:
    """Tier of a record that has some match.

    `concepts` counts the informative matched concepts (generic words excluded);
    `rarest_df` is the document frequency of the rarest informative matched term, or None;
    `n` is the number of candidate records.
    """
    if in_scope or semantic:
        return STRONG
    if concepts >= 2:
        return STRONG
    if concepts == 1 and rarest_df is not None:
        share = RARE_SHARE if rare_share is None else rare_share
        return MEDIUM if rarest_df <= max(distinctive_df, share * n) else WEAK
    return WEAK
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_evidence.py -q -p no:cacheprovider`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/retrieval/evidence.py tests/test_evidence.py
git commit -m "retrieval: evidence tiers as a pure function (not wired yet)"
```

---

### Task 14: Lever 1, drop weak-only records from `recall()`; tune `RARE_SHARE` on dev

**Files:**
- Modify: `src/agent_memory/retrieval/retriever.py` (imports; the non-strict branch of `passes`; the survivors line)
- Create: `tests/test_evidence_floor.py`
- Modify: `tests/eval/bench/RESULTS.md` (trial row and the share grid)

**Interfaces:**
- Consumes: `evidence.classify`, `evidence.GENERIC_STEMS`, `evidence.SMALL_STORE`, `evidence.WEAK`, `_concepts`, `bm25.df`, `bm25.n`.
- Produces: the `tiers: Dict[NodeRef, str]` map inside `_rank` (Tasks 16 and 18 read it).

The strict (`check()`) branch of `passes` does not change.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_evidence_floor.py`:

```python
from pathlib import Path

from agent_memory import AgentMemory


def conv(m, category, rule, **kw):
    return m.remember("convention", {"category": category, "rule": rule,
                                     "enforced_by": "manual", "exceptions": ""}, **kw)


def store(tmp_path, order=None, fillers=30, common=8):
    """`common` records share the word "process"; `fillers` records share nothing.
    Forty records in all (with the one rare-word note and the pinned rule), so the store is not small."""
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    m = AgentMemory("t", repo_root=str(tmp_path))
    steps = []
    for n in range(common):
        steps.append(lambda n=n: conv(m, f"area{n}", f"process step{n} runs through widget{n}",
                                      scope=[f"svc{n}/**"]))
    for n in range(fillers):
        steps.append(lambda n=n: conv(m, f"fill{n}", f"zebra{n} policy gadget{n}",
                                      scope=[f"filler/{n}/*"]))
    steps.append(lambda: conv(m, "audio", "xylophone tuning drifts under load"))
    steps.append(lambda: m.remember("convention", {"category": "security",
                                                   "rule": "never log secrets", "enforced_by": "manual",
                                                   "exceptions": ""}, priority="must", scope=["*"]))
    for i in (order or range(len(steps))):
        steps[i]()
    return m


def ids(result):
    return [i.record.id for i in result.items]


def texts(result):
    return " ".join(i.text for i in result.items)


def test_a_record_matched_only_by_a_common_word_is_dropped(tmp_path):
    m = store(tmp_path)
    result = m.recall(task="process image uploads", files=[])
    assert "widget" not in texts(result)
    assert "never log secrets" in texts(result)             # the pinned rule still arrives


def test_a_record_matched_by_a_rare_word_is_kept(tmp_path):
    m = store(tmp_path)
    assert "xylophone" in texts(m.recall(task="xylophone tuning", files=[]))


def test_two_informative_concepts_keep_a_record_even_if_each_word_is_common(tmp_path):
    m = store(tmp_path)
    result = m.recall(task="process step3 widget3", files=[])
    assert "step3" in texts(result)


def test_a_generic_task_with_a_file_in_scope_still_returns_the_scoped_record(tmp_path):
    """Review focus 1: scope is strong evidence, whatever the words are."""
    m = store(tmp_path)
    result = m.recall(task="process it", files=["svc3/handler.py"])
    assert "step3" in texts(result)
    assert "step4" not in texts(result)


def test_the_pinned_rule_is_unaffected_by_the_tiers(tmp_path):
    """Review focus 2."""
    m = store(tmp_path)
    assert "never log secrets" in texts(m.recall(task="process", files=[]))
    assert "never log secrets" in texts(m.recall(task="", files=[]))


def test_results_do_not_depend_on_the_order_records_were_written(tmp_path):
    """Review focus 3."""
    forward = store(tmp_path / "a")
    total = 8 + 30 + 2
    backward = store(tmp_path / "b", order=list(reversed(range(total))))
    for task, files in (("process image uploads", []), ("xylophone tuning", []),
                        ("process it", ["svc3/handler.py"])):
        a = sorted(i.text for i in forward.recall(task=task, files=files).items)
        b = sorted(i.text for i in backward.recall(task=task, files=files).items)
        assert a == b, (task, a, b)


def test_check_keeps_its_own_floor(tmp_path):
    m = AgentMemory("t", repo_root=str(tmp_path))
    m.remember("bug_pattern", {"name": "retry_storm",
                               "signature": "unbounded retry of a failing webhook floods the provider"})
    assert m.check("retry the webhook forever")             # two concepts: reported
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_evidence_floor.py -q -p no:cacheprovider`
Expected: `test_a_record_matched_only_by_a_common_word_is_dropped` fails (the common-word records are returned today); the others may pass.

- [ ] **Step 3: Wire the tiers into `_rank`**

In `src/agent_memory/retrieval/retriever.py`, add to the imports next to `from .channels import ...`:

```python
from .evidence import GENERIC_STEMS, SMALL_STORE, WEAK, classify
```

Replace the non-strict `return` in `passes`, and add the `tiers` map before it, so the block reads:

```python
        tiers: Dict[NodeRef, str] = {}

        def passes(ref: NodeRef) -> bool:
            matched = hits.get(ref, _NONE)
            in_scope = scope.get(ref, 0.0) >= SCOPE_FLOOR
            if strict:
                distinctive = any(bm25.df[t] <= DISTINCTIVE_DF for t in matched)
                count = _concepts(matched, words)
                return count >= 2 or (count == 1 and (in_scope or distinctive))
            if not (matched or in_scope or ref in semantic):
                return False
            informative = matched - GENERIC_STEMS
            tiers[ref] = classify(
                in_scope=in_scope, semantic=ref in semantic,
                concepts=_concepts(informative, words),
                rarest_df=min((bm25.df[t] for t in informative), default=None),
                n=bm25.n, distinctive_df=DISTINCTIVE_DF)
            return tiers[ref] != WEAK or bm25.n < SMALL_STORE
```

(`SMALL_STORE` is 0 for now, so the small-store clause never fires; Task 15 turns it on.)

- [ ] **Step 4: Run the new tests and the existing suite**

Run: `python -m pytest tests/test_evidence_floor.py tests/test_evidence.py tests/test_retriever.py tests/test_check.py tests/eval -q -p no:cacheprovider`
Expected: the new tests pass. If an existing retriever or eval test now fails (a record that used to survive on one shared word), do not edit that test: the in-sample scenarios are the guardrail. Report which scenario and which record, and try the `RARE_SHARE` grid below before concluding.

- [ ] **Step 5: Tune `RARE_SHARE` on the dev split, once**

For each share in `0.02, 0.04, 0.06, 0.10, 0.15` run the dev split with the constant set for the run only:

```bash
for s in 0.02 0.04 0.06 0.10 0.15; do
  echo "share $s"
  PYTHONPATH=src python -c "
import agent_memory.retrieval.evidence as e; e.RARE_SHARE = $s
import sys; sys.argv = ['run', '--split', 'dev', '--label', 'grid', '--against', 'baseline']
from tests.eval.bench import run; raise SystemExit(run.main())"
done
```

Choose the share with the smallest dev noise tokens such that dev must-include recall is at least the baseline's (rule 1's no-fall condition); on a tie choose the larger share. Set `RARE_SHARE` in `evidence.py` to that value. Put the whole grid (share, recall, noise tokens per task, clean rate) in `RESULTS.md` under "Lever trials (dev split)". Do not run the test split.

- [ ] **Step 6: Record the trial and check the in-sample scorecard**

Run:

```bash
PYTHONPATH=src python -m tests.eval.bench.run --split dev --label lever1 --against baseline
PYTHONPATH=src python -m tests.eval.run --service
```

Add a `lever 1: drop weak-only, RARE_SHARE=<chosen>` row to the trial table (recall, noise tokens per task, clean-abstain, leaks). The main scorecard must still read recall 1.00 (30/30), leaks 0, abstention 1.00; report the held-out figure.

- [ ] **Step 7: Run the full suite and commit**

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: all pass.

```bash
git add src/agent_memory/retrieval/retriever.py src/agent_memory/retrieval/evidence.py tests/test_evidence_floor.py tests/eval/bench/RESULTS.md
git commit -m "retrieval: drop weak-only matches from recall (lever 1)" -m "dev: recall <r>, noise tokens/task <n>, clean-abstain <c>, RARE_SHARE <s>"
```

(Fill the numbers into the commit body from the run.)

---

### Task 15: Lever 5, the small-store rule

**Files:**
- Modify: `src/agent_memory/retrieval/evidence.py` (`SMALL_STORE = 25`)
- Modify: `tests/test_evidence_floor.py` (append)
- Modify: `tests/eval/bench/RESULTS.md`

**Interfaces:**
- Consumes: `bm25.n < SMALL_STORE` already read in `passes` (Task 14).

The benchmark worlds all hold at least 75 records, so this constant cannot be tuned on them. It is set by reasoning (a store of fewer than 25 notes costs little to over-show and a real match is worth more) and pinned by a boundary test. Record that in `RESULTS.md` under "Not measured by the benchmark".

- [ ] **Step 1: Write the failing tests** (append to `tests/test_evidence_floor.py`):

```python
def test_a_store_below_the_small_store_size_keeps_weak_matches(tmp_path):
    """Review focus 4: a new user's first notes are not dropped for being 'weak'."""
    small = AgentMemory("s", repo_root=str(tmp_path / "small"))
    for n in range(8):
        conv(small, f"a{n}", f"process step{n} runs through widget{n}")
    assert "widget" in texts(small.recall(task="process image uploads", files=[]))


def test_the_boundary_is_exact(tmp_path):
    from agent_memory.retrieval import evidence
    n = evidence.SMALL_STORE
    at = AgentMemory("at", repo_root=str(tmp_path / "at"))
    for k in range(n):                      # exactly SMALL_STORE records: not small any more
        conv(at, f"a{k}", f"process step{k} runs through widget{k}")
    below = AgentMemory("below", repo_root=str(tmp_path / "below"))
    for k in range(n - 1):
        conv(below, f"a{k}", f"process step{k} runs through widget{k}")
    assert "widget" not in texts(at.recall(task="process image uploads", files=[]))
    assert "widget" in texts(below.recall(task="process image uploads", files=[]))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_evidence_floor.py -q -p no:cacheprovider`
Expected: the two new tests fail (`SMALL_STORE` is 0, so a small store drops weak matches, and `n` is 0).

- [ ] **Step 3: Set `SMALL_STORE = 25`** in `src/agent_memory/retrieval/evidence.py`.

- [ ] **Step 4: Run the tests, the existing suite and the dev split**

```bash
python -m pytest tests/test_evidence_floor.py tests/test_evidence.py tests/test_retriever.py tests/eval -q -p no:cacheprovider
PYTHONPATH=src python -m tests.eval.bench.run --split dev --label lever5 --against baseline
PYTHONPATH=src python -m tests.eval.run --service
```

Expected: tests pass; the dev numbers equal lever 1's (every world has at least 75 records) and the main scorecard is unchanged. If the 40-record shopline scorecard moves, the constant interacts with the in-sample world: report it.

- [ ] **Step 5: Record and commit**

Add the `lever 5` row (expected identical to lever 1) and the "Not measured by the benchmark" note to `RESULTS.md`. Run the full suite, then:

```bash
git add src/agent_memory/retrieval/evidence.py tests/test_evidence_floor.py tests/eval/bench/RESULTS.md
git commit -m "retrieval: keep weak matches in stores under 25 records (lever 5)" -m "dev unchanged from lever 1; boundary pinned by test"
```

---

### Task 16: Lever 2, graph seeds from strong and medium survivors only

**Files:**
- Modify: `src/agent_memory/retrieval/retriever.py` (the graph-seed block of `_rank`)
- Modify: `tests/test_evidence_floor.py` (append)
- Modify: `tests/eval/bench/RESULTS.md`

**Interfaces:**
- Consumes: `tiers` (Task 14).

Under lever 1 in a store of 25 or more records a weak record never survives, so it cannot seed; this lever only matters where weak survivors exist (a small store, and the tail arm of Task 18). It is still the right rule, and it is measured honestly: the dev split is expected not to move.

- [ ] **Step 1: Write the failing test** (append):

```python
def test_a_weak_survivor_does_not_pull_its_neighbours_in(tmp_path):
    m = AgentMemory("g", repo_root=str(tmp_path))
    weak = m.remember("bug_pattern", {"name": "a", "signature": "process step widget leaks"})
    fix = m.remember("fix_pattern", {"fix_template": "zzyzx remedy", "caveats": ""})
    m.graph.add_edge("FIX_FOR", fix.ref, weak.ref)
    for n in range(6):                      # a small store, so the weak match survives
        conv(m, f"c{n}", f"process step{n} runs through widget{n}")
    result = m.recall(task="process image uploads", files=[])
    assert "widget leaks" in texts(result)          # the weak record itself is shown (small store)
    assert "zzyzx" not in texts(result)             # but its fix is not dragged in
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_evidence_floor.py -q -p no:cacheprovider`
Expected: the new test fails (`zzyzx remedy` arrives by graph hop today).

- [ ] **Step 3: Change the seed block**

In `_rank`, replace:

```python
        if not strict and survivors:
            # the top GRAPH_SEEDS, ties broken by ref so the result never depends on dict order
            floor = heapq.nlargest(GRAPH_SEEDS, survivors.values())[-1]
            seeds = dict(sorted(((ref, v) for ref, v in survivors.items() if v >= floor),
                                key=lambda kv: (-kv[1], str(kv[0])))[:GRAPH_SEEDS])
```

with:

```python
        pool = {ref: v for ref, v in survivors.items() if tiers.get(ref) != WEAK}
        if not strict and pool:
            # the top GRAPH_SEEDS of the strong and medium survivors, ties broken by ref so the
            # result never depends on dict order; a weak match does not pull its neighbours in
            floor = heapq.nlargest(GRAPH_SEEDS, pool.values())[-1]
            seeds = dict(sorted(((ref, v) for ref, v in pool.items() if v >= floor),
                                key=lambda kv: (-kv[1], str(kv[0])))[:GRAPH_SEEDS])
```

Keep the lines that follow (the `graph_neighbours` loop) as they are.

- [ ] **Step 4: Run the tests, the suite and the dev split**

```bash
python -m pytest tests/test_evidence_floor.py tests/test_retriever.py tests/test_channels_render.py tests/eval -q -p no:cacheprovider
PYTHONPATH=src python -m tests.eval.bench.run --split dev --label lever2 --against baseline
PYTHONPATH=src python -m tests.eval.run --service
```

Expected: pass; dev equals lever 5's numbers (state this plainly in `RESULTS.md` if so: "no effect on the benchmark; kept because weak survivors exist in small stores and in the tail arm").

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/retrieval/retriever.py tests/test_evidence_floor.py tests/eval/bench/RESULTS.md
git commit -m "retrieval: graph seeds come from strong and medium matches only (lever 2)" -m "dev <numbers or 'unchanged'>"
```

---

### Task 17: Lever 3, the generic task-word list

**Files:**
- Modify: `src/agent_memory/retrieval/evidence.py` (`GENERIC_WORDS`)
- Modify: `tests/test_evidence.py` (append the exact-set test)
- Modify: `tests/test_evidence_floor.py` (append)
- Modify: `tests/eval/bench/RESULTS.md`

**Pre-registered starting list** (committed with this plan, before the first test-split run in Task 12):

```
write handle configure refactor improve export import process document set setup run build
create remove delete implement support enable disable get show display load save read parse
format convert move rename clean optimize speed
```

Dev-split edits are allowed (add or remove a word, each recorded with the dev numbers that justified it). The test split is never consulted.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evidence.py`:

```python
def test_the_generic_word_list_is_exactly_the_registered_one():
    registered = set("""write handle configure refactor improve export import process document set
    setup run build create remove delete implement support enable disable get show display load
    save read parse format convert move rename clean optimize speed""".split())
    assert set(evidence.GENERIC_WORDS) == registered, (
        "the list changed: record each dev-split edit in tests/eval/bench/RESULTS.md and update "
        "this test with it")
    assert evidence.GENERIC_STEMS == evidence.stems(registered)
```

Append to `tests/test_evidence_floor.py`:

```python
def test_a_generic_word_alone_does_not_keep_a_record_even_when_it_is_rare(tmp_path):
    m = store(tmp_path)
    m.remember("decision", {"title": "Billing export writes CSV nightly", "context": "finance pulls it"})
    result = m.recall(task="write a script to resize images", files=[])
    assert "Billing export" not in texts(result)


def test_a_generic_word_still_counts_with_a_second_concept(tmp_path):
    m = store(tmp_path)
    m.remember("decision", {"title": "Billing export writes CSV nightly", "context": "finance pulls it"})
    result = m.recall(task="write the billing csv", files=[])
    assert "Billing export" in texts(result)
```

(The second test: `billing` and `csv` are two informative concepts, so the record is strong; `write` adds nothing.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_evidence.py tests/test_evidence_floor.py -q -p no:cacheprovider`
Expected: the exact-set test and the first new floor test fail (list empty; `writ` is rare so the record survives).

- [ ] **Step 3: Fill the list.** In `evidence.py` replace `GENERIC_WORDS: FrozenSet[str] = frozenset()` with:

```python
GENERIC_WORDS: FrozenSet[str] = frozenset("""
write handle configure refactor improve export import process document set setup run build
create remove delete implement support enable disable get show display load save read parse
format convert move rename clean optimize speed
""".split())
```

- [ ] **Step 4: The dev trial.** Run:

```bash
python -m pytest tests/test_evidence.py tests/test_evidence_floor.py tests/test_retriever.py tests/test_check.py tests/eval -q -p no:cacheprovider
PYTHONPATH=src python -m tests.eval.bench.run --split dev --label lever3 --against baseline
PYTHONPATH=src python -m tests.eval.run --service
```

If dev recall fell below lever 5's, find which generic word cost a `relevant` record (the saved run lists `missed`; run with `--save --label lever3`), remove only that word, record the edit and the numbers, and update the exact-set test with it. If a dev empty task still shows noise matched on a generic word that is not in the list, you may add it, recorded the same way.

- [ ] **Step 5: Record and commit**

Add the `lever 3` row and every list edit to `RESULTS.md`. Run the full suite, then:

```bash
git add src/agent_memory/retrieval/evidence.py tests/test_evidence.py tests/test_evidence_floor.py tests/eval/bench/RESULTS.md
git commit -m "retrieval: generic task words are weak evidence on their own (lever 3)" -m "dev <numbers>; list edits: <none or list>"
```

---

### Task 18: Lever 4, the capped "possibly related" tail (an arm)

**Files:**
- Modify: `src/agent_memory/retrieval/evidence.py` (`WEAK_POLICY`, `TAIL_MAX_ITEMS`, `TAIL_BUDGET_SHARE`)
- Modify: `src/agent_memory/retrieval/retriever.py` (`ScoredMemory.weak`, `_Ranking.weak`, `_rank`, `recall`)
- Modify: `src/agent_memory/retrieval/render.py` (the tail section)
- Create: `tests/test_weak_tail.py`
- Modify: `tests/eval/bench/RESULTS.md`

**Interfaces:**
- Consumes: `tiers`, `WEAK`.
- Produces: `evidence.WEAK_POLICY = "drop"` (the default; `"tail"` selects the arm), `evidence.TAIL_MAX_ITEMS = 2`, `evidence.TAIL_BUDGET_SHARE = 0.10`; `ScoredMemory.weak: bool = False`; a `## Possibly related` section in the rendered prompt, only in tail mode and only when there are weak items.

In tail mode a weak match is not dropped: it goes after every other item, at most `TAIL_MAX_ITEMS` of them and at most `TAIL_BUDGET_SHARE` of `max_tokens`, rendered under its own header, never counted as pinned or carried. In drop mode nothing changes. Weak items still do not seed graph neighbours (Task 16) and never reach `check()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_weak_tail.py`:

```python
import pytest

from agent_memory.retrieval import evidence

from .test_evidence_floor import store, texts


@pytest.fixture
def tail(monkeypatch):
    monkeypatch.setattr(evidence, "WEAK_POLICY", "tail")


def test_drop_is_the_default(tmp_path):
    assert evidence.WEAK_POLICY == "drop"
    m = store(tmp_path)
    assert "widget" not in texts(m.recall(task="handle image uploads and process them", files=[]))


def test_tail_mode_lists_weak_matches_under_their_own_header(tmp_path, tail):
    m = store(tmp_path)
    result = m.recall(task="process image uploads", files=[])
    prompt = result.to_prompt()
    assert "## Possibly related" in prompt
    weak = [i for i in result.items if i.weak]
    assert 1 <= len(weak) <= evidence.TAIL_MAX_ITEMS
    header = prompt.index("## Possibly related")
    for item in weak:
        assert prompt.index(item.record.id) > header
    for item in result.items:
        if not item.weak:
            assert prompt.index(item.record.id) < header        # the tail comes after everything


def test_the_tail_is_capped_in_items_and_in_budget(tmp_path, tail):
    m = store(tmp_path)                      # eight weak candidates for "process"
    result = m.recall(task="process image uploads", files=[], max_tokens=2000)
    assert sum(1 for i in result.items if i.weak) <= evidence.TAIL_MAX_ITEMS
    tight = m.recall(task="process image uploads", files=[], max_tokens=60)
    tail_cost = sum(m.count_tokens(i.text) for i in tight.items if i.weak)
    assert tail_cost <= 0.10 * 60 + 1


def test_a_strong_match_never_sits_in_the_tail(tmp_path, tail):
    m = store(tmp_path)
    result = m.recall(task="xylophone tuning", files=[])
    assert all(not i.weak for i in result.items if "xylophone" in i.text)


def test_the_header_is_absent_when_there_is_no_weak_item(tmp_path, tail):
    m = store(tmp_path)
    assert "## Possibly related" not in m.recall(task="xylophone tuning", files=[]).to_prompt()


def test_weak_items_do_not_seed_graph_neighbours_in_tail_mode(tmp_path, tail):
    m = store(tmp_path)                      # "process" is in eight records: a weak word here
    weak = m.remember("bug_pattern", {"name": "audit", "signature": "process audit trail leaks"})
    fix = m.remember("fix_pattern", {"fix_template": "zzyzx remedy", "caveats": "none"})
    m.graph.add_edge("FIX_FOR", fix.ref, weak.ref)
    result = m.recall(task="process image uploads", files=[])
    assert "zzyzx" not in texts(result)
```

The contract the tests pin: at most two weak items, at most ten percent of the budget, all under the header and after everything else, none seeding graph neighbours, and `drop` (the default) unchanged.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_weak_tail.py -q -p no:cacheprovider`
Expected: attribute errors for `WEAK_POLICY`, `TAIL_MAX_ITEMS`, `weak`.

- [ ] **Step 3: Implement**

`evidence.py`: add

```python
WEAK_POLICY = "drop"        # "drop": weak-only matches are not returned; "tail": a capped tail
TAIL_MAX_ITEMS = 2
TAIL_BUDGET_SHARE = 0.10
```

`retriever.py`: read the policy at call time as `evidence.WEAK_POLICY` (add `from . import evidence`), not through a name imported at module load: the tests and the dev trial set it on the module.
1. `ScoredMemory` gains `weak: bool = False` (after the existing defaulted fields; keep `to_dict` unchanged).
2. `_Ranking.__init__(self, refs, build, weak=())` stores `self.weak = list(weak)`.
3. In `_rank`, in the non-strict `passes`, keep weak refs as survivors when `WEAK_POLICY == "tail"`: `return tiers[ref] != WEAK or bm25.n < SMALL_STORE or WEAK_POLICY == "tail"`. After `kept = self._dedup_texts(...)`, split the kept refs: `weak_set = {r for r in tiers if tiers[r] == WEAK and bm25.n >= SMALL_STORE}` (empty in drop mode and in small stores, where weak matches are ordinary survivors); `main = [order[i] for i in kept if order[i] not in weak_set]`, `tail = [order[i] for i in kept if order[i] in weak_set]`; return `_Ranking(main, build, tail)`; `build` sets `weak=ref in weak_set` on the `ScoredMemory`.
4. `rank()` (public) keeps returning only `ranking.refs`, so `check()` and other callers never see the tail.
5. In `recall`, after `pack(carried)`, pack the tail: take `ranking.weak`, skip any ref already in `taken`/selected, then add at most `TAIL_MAX_ITEMS` items whose cumulative rendered cost is at most `int(max_tokens * TAIL_BUDGET_SHARE)` and that also fit the remaining `budget`.

`render.py`: `render()` splits `items` into those with `getattr(i, "weak", False)` and the rest; the rest render as before; if any weak items remain, append `## Possibly related` and their lines (via `render_item`). Add `"## Possibly related"` to nothing in `HEADERS` (it is not a tier), but count its header tokens in `recall`'s budget only when the tail is non-empty.

- [ ] **Step 4: Run the tests, the suite, and both arms on dev**

```bash
python -m pytest tests/test_weak_tail.py tests/test_evidence_floor.py tests/test_retriever.py tests/test_channels_render.py tests/eval -q -p no:cacheprovider
PYTHONPATH=src python -m tests.eval.bench.run --split dev --label drop --against baseline
PYTHONPATH=src python -c "
import agent_memory.retrieval.evidence as e; e.WEAK_POLICY = 'tail'
import sys; sys.argv = ['run', '--split', 'dev', '--label', 'tail', '--against', 'baseline']
from tests.eval.bench import run; raise SystemExit(run.main())"
```

The default (`drop`) must produce exactly lever 3's dev numbers. Record both arms in `RESULTS.md`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/retrieval tests/test_weak_tail.py tests/eval/bench/RESULTS.md
git commit -m "retrieval: capped possibly-related tail as an opt-in arm (lever 4)" -m "dev drop <numbers>; dev tail <numbers>; default stays drop"
```

---

### Task 19: Lever 6 trial and the semantic arm (measurements only)

**Files:**
- Modify: `tests/eval/bench/RESULTS.md`
- Create (scratch only, not committed): trial scripts in the scratchpad directory

No change to `src/`. Two measurements, both on the dev split, both recorded.

- [ ] **Step 1: The rank-relative score cut (ruled out in advance).** In a scratch script, monkeypatch `Retriever._rank` (or wrap `recall`) so that, after the floor, items scoring below a fraction `f` of the best non-pinned score are dropped, for `f` in `0.25, 0.5, 0.75`. Run the dev split for each. Record recall and noise tokens. Expected: the cut loses `relevant` records before it removes noise, as the score ranges in the spec's diagnosis suggest. If it does not, say so and report it as a finding for the user; do not ship it.

- [ ] **Step 2: The semantic arm.** Check availability:

```bash
python -c "import ison_graph_embeddings, sys; print('ok')"
```

If it imports, run the dev split with the embedding channel attached to each built world (`memory.attach_semantic(create_graph_embedded_memory(memory).` as in `tests/test_embeddings_integration.py`, using the library's default pinned encoder; record the model name and version) for the final retriever, and compare to lever 3 (and to the tail arm if you can). If it does not import (as of 2026-11-21 it does not in this environment, although a stale editable install is listed by pip), write "not measured: `ison_graph_embeddings` is not importable here" and the exact command to repeat it, and stop. Do not install anything.

- [ ] **Step 3: Record and commit**

Add both results to `RESULTS.md` (a "Lever 6" row and a "Semantic arm" section, with the model if one ran). Commit:

```bash
git add tests/eval/bench/RESULTS.md
git commit -m "docs: record the rank-cut trial and the semantic arm on dev"
```

---

### Task 20: Decide drop versus tail, run the test split, document, add the dev guard

**Files:**
- Modify: `src/agent_memory/retrieval/*` (remove the losing policy)
- Modify: `tests/eval/bench/RESULTS.md`, `tests/eval/RESULTS.md`, `README.md`, `docs/superpowers/specs/2026-11-21-recall-quality-design.md` (status line and results), `src/agent_memory/protocol.py` and the installer goldens only if the tail is adopted
- Create: `tests/eval/test_bench_dev.py`

**Pre-registered decision rule.** Adopt the tail iff, on the dev split, `drop` has recall below the baseline's AND `tail` brings recall back to within 0.5 percentage points of baseline while keeping the noise-token cut at 50% or more. Otherwise keep `drop`.

- [ ] **Step 1: Apply the rule** to the two dev rows from Task 18 and write the decision, with the numbers, in `RESULTS.md`.

- [ ] **Step 2: Remove the loser.** If `drop` wins: delete `WEAK_POLICY`, `TAIL_*`, `ScoredMemory.weak`, `_Ranking.weak`, the tail packing and the render section, and `tests/test_weak_tail.py` (the drop-by-default and small-store behaviour stay covered by `tests/test_evidence_floor.py`). If `tail` wins: make `"tail"` the default constant, update the protocol block text and the installer goldens in the same commit, and document the new section in `README.md`.

- [ ] **Step 3: The one test-split run.** Only now:

```bash
PYTHONPATH=src python -m tests.eval.bench.run --split test --label after --save --against baseline
```

Evaluate the spec's acceptance rules, with the runner's four (recall, leaks, noise, clean) plus: rule 5 (run `python -m pytest tests/eval/test_thresholds.py::test_recall_latency_guard -q -p no:cacheprovider`, and repeat the median script from Task 12 Step 5 and report both medians side by side, noting the CPU throttling caveat), rule 6 (`git diff main --stat -- tests/eval/scenarios.py tests/eval/world.py tests/eval/heldout.py tests/eval/harness.py tests/eval/run.py` is empty), rule 7 (the full suite). A rule that fails is reported as failed with its numbers; the rule is not moved and the test split is not run a third time.

- [ ] **Step 4: Report the held-out figure** (original and relabelled) from `PYTHONPATH=src python -m tests.eval.run --service` and the relabel script of Task 12, and the main scorecard (recall 1.00, leaks 0, abstention 1.00 must still hold).

- [ ] **Step 5: Write the results.** `tests/eval/bench/RESULTS.md` gets the final test-split table with Wilson intervals, the rule table, the lever trial table, the decision, "Not measured by the benchmark" and "Known limits". `tests/eval/RESULTS.md` gets a short section pointing to it with the held-out before and after. The spec's status line becomes `implemented`, and its results section is filled in. End the report with a recommendation on whether the semantic channel should become a default (from Task 19), left as the user's decision.

- [ ] **Step 6: The dev guard.** Create `tests/eval/test_bench_dev.py`: it runs `run.run_split("dev")` once (module fixture), asserts must-include recall is at least the final dev recall minus 0.01, leaks are 0, and mean noise tokens per task are at most the final dev value times 1.15 (rounded up to a whole token). It does not run the test split.

- [ ] **Step 7: Full suite and commits**

Run: `python -m pytest tests -q -p no:cacheprovider` (expected: all pass, `test_recall_latency_guard` re-run alone if it flakes). Commit the retriever cleanup, then the docs and guard separately:

```bash
git add src tests
git commit -m "retrieval: keep <drop|tail> as the weak-match policy"
git add tests/eval README.md docs
git commit -m "docs: recall quality results, held-out figures and the dev guard"
```
