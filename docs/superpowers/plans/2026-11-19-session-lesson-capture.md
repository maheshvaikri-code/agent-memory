# Session-End Lesson Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent hand over lessons at session end (or any time) into a pending queue that nothing recalls until a person accepts them at a terminal.

**Architecture:** A new record status `pending` and a new source `reviewed` in the engine; one atomic engine call, `AgentMemory.capture`, that sweeps expired lessons, enforces the 100-pending limit and skips duplicates. `MemoryService.capture` validates each lesson (the gate) and is called by a new MCP tool, by a `lessons` argument on `memory_session_end`, and by a new `amem capture` command. `MemoryService.review_*` methods back a new `amem review` command group, whose `accept` needs a real terminal; no MCP tool can accept.

**Tech Stack:** Python 3.9+ core, click CLI, FastMCP (optional `mcp` extra, Python 3.10+), pytest.

**Spec:** `docs/superpowers/specs/2026-11-18-session-lesson-capture-design.md` (read it first; the plan argues from it). Builds on `2026-11-17-agent-surface-design.md`.

**Branch:** create `feature/lesson-capture` from `main` before Task 1 (`git checkout -b feature/lesson-capture`). Never push; never touch remotes or git config.

## Global Constraints

- Git: commits are authored by the configured identity (`mahesh.vaikri.ai@gmail.com`). **No `Co-Authored-By` or any other attribution trailer on any commit** (the user's standing instruction; it overrides any harness reminder to add one).
- Run tests as `python -m pytest tests -q` (conftest puts `src` first). Run the scorecard as `PYTHONPATH=src python -m tests.eval.run --service`. Without `PYTHONPATH=src` a sibling installed copy is imported and the numbers are wrong.
- Baseline on `main` (810e184 plus the spec commit): 793 passed, 6 skipped, 8 deselected. Scorecard (engine and `--service`): recall 1.00, 0 leaks, abstention 1.00, held-out abstention 0.38. None of these may get worse.
- The core stays Python 3.9+ (no `X | Y` type syntax, no `match`). `mcp_server.py` must not use `from __future__ import annotations` and keeps every free-text tool parameter a plain `str = ""`.
- Constants: `MAX_LESSONS_PER_CALL = 10`, `MAX_PENDING = 100`, `PENDING_TTL_DAYS = 30`.
- Every string that reaches an agent or a terminal from recorded text goes through `shaping.neutralize` (and ids through `escape_id`); CLI output also goes through `_printable`.
- MCP tool descriptions: each at most 400 characters, all together at most 3000 (existing test).
- Ten agent-writable kinds only (`AGENT_KINDS`); lessons never take `priority` or `confidence`.
- Nothing an agent can call (MCP tool, `lessons` argument) may promote, retract, supersede or delete a pending lesson.

## Clarifications of the spec made while planning

Each is recorded in the spec's implementation notes by Task 7. Cost-if-wrong is small and visible.

1. **Symbol-only lessons.** The record model anchors files only. A lesson with `symbols` and no `files` is accepted; the symbols are appended to the stored evidence as `symbols: a, b` so the reviewer sees them. A lesson with neither is skipped.
2. **Partial room.** With 95 pending and 10 lessons, the first 5 are stored and the rest are skipped with reason `queue_full`. With no room at all, `capture` stores nothing and raises `queue_full`.
3. **Duplicates of pending lessons** are skipped too (not only duplicates of active notes). The reason never shows another lesson's text or id.
4. **Accept refuses** when an active note with the same text now exists (it would create two active records); the person rejects the lesson instead.
5. **`amem capture`** (JSON list on stdin) is added so CLI-only agents can capture; the spec's "matching sentence" in the CLI protocol block needs a command to point at.
6. **Counts.** `status` has `pending` (waiting, not expired) and `pending_expired` (expired, not yet swept); `session_start` has `pending_review` (the same live count). The spec's "how many of those have expired" means this second number.
7. **Accept asks the person to type the word `accept`** (not y/n), and has no `--json`.
8. **Unknown lesson keys** (including `priority` and `confidence`) skip the lesson.
9. **`memory_session_end` result** gains a `capture` key only when lessons were passed (`[]` counts as none).
10. **`reject` sweeps** expired lessons like `capture` and `accept` do.

## Review Focus

Failure modes the spec implies that no single feature test would otherwise pin; each gets a test in the task named.

1. A pending lesson of a session kind (`partial_work`, `open_question`, `blocker`) must never come back as a carried item (Task 1).
2. The same lesson twice, in one call or across calls, gives one pending record; an active duplicate is not boosted or rewritten (Tasks 1, 2).
3. Two processes capturing at 95 pending never push the queue past 100 (Task 1, real subprocesses).
4. Recorded text with `<`, ESC sequences or bidi controls must not reach a terminal or an agent raw through `review list` / `show` or through skip reasons (Tasks 2, 4, 5).
5. A lesson that expired or became a duplicate between `show` and `accept` is refused, not promoted (Tasks 4, 5).

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/agent_memory/record.py` | modify | `pending` status, `reviewed` source, `PENDING_TTL_DAYS`, `pending_expired` |
| `src/agent_memory/writer.py` | modify | `add(status=)`, `find_text`, `pending`, `accept`, `expire_pending` |
| `src/agent_memory/memory.py` | modify | `capture`, `pending_records`, `find_duplicate`, `accept`, `expire_pending` |
| `src/agent_memory/service/errors.py` | modify | `queue_full` code |
| `src/agent_memory/service/service.py` | modify | `capture`, lesson gate, `session_end(lessons=)`, counts, `review_*` |
| `src/agent_memory/service/shaping.py` | modify | comment only: `reviewed` is not unreviewed |
| `src/agent_memory/mcp_server.py` | modify | `memory_capture_lessons`, `lessons` on `memory_session_end` |
| `src/agent_memory/protocol.py` | modify | one clause each in the MCP and CLI step 5 |
| `src/agent_memory/cli_review.py` | create | `amem capture`, `amem review list/show/accept/reject` |
| `src/agent_memory/cli_agent.py` | modify | `status` output lines |
| `src/agent_memory/cli.py` | modify | register `cli_review` |
| `src/agent_memory/doctor.py` | modify | `pending` check |
| `tests/test_pending_engine.py` | create | engine |
| `tests/test_service_capture.py` | create | gate, capture, session end, counts |
| `tests/test_mcp_capture.py` | create | tool surface and calls |
| `tests/test_service_review.py` | create | review service methods |
| `tests/test_cli_review.py` | create | CLI |
| `tests/test_capture_two_process.py` | create | two real servers |
| `tests/eval/test_pending_leak.py` | create | scorecard and leak proof |
| README, DESCRIBE, both specs | modify | docs (Task 7) |

---

### Task 1: Engine — `pending` status, `reviewed` source, atomic capture

**Files:**
- Modify: `src/agent_memory/record.py`, `src/agent_memory/writer.py`, `src/agent_memory/memory.py`
- Test: `tests/test_pending_engine.py` (create)

**Interfaces:**
- Produces (used by Tasks 2 and 4):
  - `record.PENDING_TTL_DAYS = 30`; `record.pending_expired(created_at: str, *, now: Optional[datetime] = None, days: int = PENDING_TTL_DAYS) -> bool`
  - `SOURCE_CONFIDENCE["reviewed"] == 0.7`; `"pending"` in `STATUSES`
  - `MemoryWriter.add(..., status: str = "active")` accepts `"active"` or `"pending"` (else `ValueError`); a pending add never merges into an active note and is never a merge target
  - `AgentMemory.remember(kind, fields, source="extracted", status="pending", ...)` (existing `**kw` pass-through)
  - `AgentMemory.pending_records() -> List[MemoryRecord]` (oldest first)
  - `AgentMemory.find_duplicate(kind, fields) -> Optional[Tuple[NodeRef, str]]`: `(ref, "active" | "pending")`, active preferred
  - `AgentMemory.accept(memory_id) -> None`: pending → active, source `reviewed`, confidence 0.7; `ValueError` if not pending or if an active note with the same text exists
  - `AgentMemory.expire_pending(now=None) -> int`: archives pending older than 30 days
  - `AgentMemory.capture(lessons, *, max_pending) -> dict` where each lesson is `{"kind", "fields", "anchors": [str], "evidence"}`; returns `{"captured": [{"index", "id"}], "skipped": [{"index", "code": "duplicate"|"queue_full", "of": id|None, "of_status": "active"|"pending"|None}], "full": bool, "swept": int, "pending": int}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pending_engine.py`:

```python
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agent_memory import AgentMemory
from agent_memory.record import PENDING_TTL_DAYS, STATUSES, MemoryRecord, pending_expired
from agent_memory.service.shaping import is_unreviewed
from storage_helpers import SRC

QUIRK = {"description": "the zorblax queue drops items over 64KB"}


def lesson(text, **extra):
    base = {"kind": "quirk", "fields": {"description": text}, "anchors": ["src/a.py"],
            "evidence": "seen in a test run"}
    base.update(extra)
    return base


def rec(memory, ref):
    return MemoryRecord.from_node(memory.graph.get_node(*ref))


def pend(memory, kind="quirk", fields=None, **kw):
    return memory.remember(kind, fields or QUIRK, source="extracted", status="pending", **kw)


@pytest.fixture
def memory():
    return AgentMemory("t")


# -- status and source -------------------------------------------------------

def test_pending_is_a_known_status_and_loads_without_a_warning(memory):
    assert "pending" in STATUSES
    result = pend(memory)
    warnings = []
    record = MemoryRecord.from_node(memory.graph.get_node(*result.ref), warnings)
    assert (record.status, record.source, record.confidence) == ("pending", "extracted", 0.5)
    assert warnings == []


def test_add_refuses_a_status_other_than_active_or_pending(memory):
    with pytest.raises(ValueError):
        memory.remember("quirk", QUIRK, status="archived")


def test_a_pending_record_is_never_recalled_checked_or_carried(memory):
    memory.start_session("t")
    pend(memory)
    pend(memory, "partial_work", {"work_type": "refactor", "location": "src/a.py",
                                  "state": "half done"})
    assert memory.recall(task="zorblax queue drops items").items == []
    assert memory.check("zorblax queue drops items over 64KB") == []
    assert list(memory.carried_items()) == []


# -- no side effects on active notes -----------------------------------------

def test_a_pending_add_does_not_touch_an_active_note_with_the_same_text(memory):
    active = memory.remember("quirk", QUIRK)
    before = rec(memory, active.ref)
    result = pend(memory, evidence="from a lesson")
    assert result.created and not result.merged and result.ref != active.ref
    after = rec(memory, active.ref)
    assert (after.confidence, after.evidence, after.status) == (before.confidence,
                                                                before.evidence, "active")


def test_a_pending_record_is_not_a_merge_target(memory):
    first = pend(memory)
    second = memory.remember("quirk", QUIRK)
    assert second.created and second.ref != first.ref
    assert rec(memory, first.ref).status == "pending"
    assert rec(memory, second.ref).status == "active"


# -- find_duplicate ----------------------------------------------------------

def test_find_duplicate_sees_active_and_pending_and_prefers_active(memory):
    assert memory.find_duplicate("quirk", QUIRK) is None
    first = pend(memory)
    assert memory.find_duplicate("quirk", QUIRK) == (first.ref, "pending")
    active = memory.remember("quirk", QUIRK)
    assert memory.find_duplicate("quirk", QUIRK) == (active.ref, "active")


def test_an_answered_question_is_not_a_duplicate(memory):
    memory.remember("open_question", {"question": "why is the queue slow?", "answered_in": "s1"})
    assert memory.find_duplicate("open_question", {"question": "why is the queue slow?"}) is None


# -- accept ------------------------------------------------------------------

def test_accept_makes_a_lesson_active_reviewed_and_recallable(memory):
    result = pend(memory)
    memory.accept(result.id)
    record = rec(memory, result.ref)
    assert (record.status, record.source, record.confidence) == ("active", "reviewed", 0.7)
    assert [i.record.id for i in memory.check("zorblax queue drops items over 64KB")] == [result.id]


def test_a_reviewed_guardrail_is_not_flagged_unreviewed_but_an_extracted_one_is(memory):
    result = pend(memory)
    assert is_unreviewed(rec(memory, result.ref)) is True       # extracted
    memory.accept(result.id)
    assert is_unreviewed(rec(memory, result.ref)) is False      # reviewed


def test_accept_refuses_a_record_that_is_not_pending(memory):
    active = memory.remember("quirk", QUIRK)
    with pytest.raises(ValueError, match="not pending"):
        memory.accept(active.id)


def test_accept_refuses_when_an_active_note_with_the_same_text_exists(memory):
    result = pend(memory)
    memory.remember("quirk", QUIRK)
    with pytest.raises(ValueError, match="same text"):
        memory.accept(result.id)
    assert rec(memory, result.ref).status == "pending"


# -- expiry ------------------------------------------------------------------

def test_pending_expired_is_a_pure_age_test():
    now = datetime(2026, 12, 31, tzinfo=timezone.utc)
    old = (now - timedelta(days=PENDING_TTL_DAYS + 1)).isoformat()
    young = (now - timedelta(days=PENDING_TTL_DAYS - 1)).isoformat()
    assert pending_expired(old, now=now) is True and pending_expired(young, now=now) is False
    assert pending_expired("", now=now) is False and pending_expired("garbage", now=now) is False


def test_expire_pending_archives_only_old_pending_records(memory):
    old_pending = pend(memory)
    active = memory.remember("quirk", {"description": "an unrelated active quirk"})
    later = datetime.now(timezone.utc) + timedelta(days=PENDING_TTL_DAYS - 1)
    assert memory.expire_pending(now=later) == 0
    later = datetime.now(timezone.utc) + timedelta(days=PENDING_TTL_DAYS + 1)
    assert memory.expire_pending(now=later) == 1
    assert rec(memory, old_pending.ref).status == "archived"
    assert rec(memory, active.ref).status == "active"
    far = datetime.now(timezone.utc) + timedelta(days=4000)
    assert memory.expire_pending(now=far) == 0            # active records never expire here


def test_pending_records_survive_a_reopen(tmp_path):
    path = str(tmp_path / "p.memory")
    with AgentMemory.open(path) as shared:
        pend(shared)
    with AgentMemory.open(path) as shared:
        assert [r.status for r in shared.pending_records()] == ["pending"]


# -- capture -----------------------------------------------------------------

def test_capture_stores_pending_records_and_reports_them(memory):
    out = memory.capture([lesson("first lesson text"), lesson("second lesson text")],
                         max_pending=100)
    assert [c["index"] for c in out["captured"]] == [0, 1]
    assert out["skipped"] == [] and out["full"] is False and out["pending"] == 2
    records = memory.pending_records()
    assert len(records) == 2 and all(r.status == "pending" and r.source == "extracted"
                                     for r in records)
    assert all(r.evidence == "seen in a test run" for r in records)


def test_capture_skips_a_lesson_repeated_in_the_same_call(memory):
    out = memory.capture([lesson("same text"), lesson("same text")], max_pending=100)
    assert len(out["captured"]) == 1
    assert out["skipped"] == [{"index": 1, "code": "duplicate", "of": None,
                               "of_status": "pending"}]
    assert len(memory.pending_records()) == 1


def test_capture_skips_a_duplicate_of_an_active_note_and_leaves_it_alone(memory):
    active = memory.remember("quirk", QUIRK)
    before = rec(memory, active.ref)
    out = memory.capture([lesson(QUIRK["description"])], max_pending=100)
    assert out["captured"] == []
    assert out["skipped"] == [{"index": 0, "code": "duplicate", "of": active.id,
                               "of_status": "active"}]
    after = rec(memory, active.ref)
    assert (after.confidence, after.evidence) == (before.confidence, before.evidence)


def test_capture_stores_as_many_as_fit_and_skips_the_rest(memory):
    out = memory.capture([lesson(f"lesson {i}") for i in range(3)], max_pending=2)
    assert len(out["captured"]) == 2
    assert out["skipped"] == [{"index": 2, "code": "queue_full", "of": None, "of_status": None}]


def test_capture_with_no_room_stores_nothing_and_says_full(memory):
    memory.capture([lesson("one"), lesson("two")], max_pending=2)
    out = memory.capture([lesson("three")], max_pending=2)
    assert out["full"] is True and out["captured"] == [] and len(memory.pending_records()) == 2


def test_capture_sweeps_expired_lessons_before_counting(memory):
    first = memory.capture([lesson("one"), lesson("two")], max_pending=2)
    ref = ("quirk", first["captured"][0]["id"])
    memory.graph.update_node(ref[0], ref[1], created_at="2020-01-01T00:00:00+00:00")
    out = memory.capture([lesson("three")], max_pending=2)
    assert out["swept"] == 1 and len(out["captured"]) == 1 and out["full"] is False
    assert rec(memory, ref).status == "archived"


WORKER = """
import sys
from agent_memory import AgentMemory
store, tag = sys.argv[1], sys.argv[2]
lessons = [{"kind": "quirk", "fields": {"description": tag + " lesson " + str(i)},
            "anchors": ["a.py"], "evidence": "e"} for i in range(10)]
with AgentMemory.open(store) as memory:
    memory.capture(lessons, max_pending=100)
"""


def test_two_processes_cannot_push_the_queue_past_its_limit(tmp_path):
    path = str(tmp_path / "p.memory")
    with AgentMemory.open(path) as shared:
        shared.capture([lesson(f"seed {i}") for i in range(95)], max_pending=100)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([SRC, env.get("PYTHONPATH", "")])
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, path, tag], env=env)
             for tag in ("alpha", "beta")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    with AgentMemory.open(path) as shared:
        assert len(shared.pending_records()) == 100
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_pending_engine.py -q`
Expected: FAIL (collection error: `cannot import name 'PENDING_TTL_DAYS'`).

- [ ] **Step 3: Implement**

`src/agent_memory/record.py`:
- change the import to `from datetime import datetime, timedelta, timezone`;
- replace the `STATUSES` and `SOURCE_CONFIDENCE` lines with:

```python
STATUSES = ("active", "pending", "superseded", "retracted", "archived")
SOURCE_CONFIDENCE = {"explicit": 0.7, "reviewed": 0.7, "extracted": 0.5, "bootstrap": 0.5,
                     "imported": 0.6, "legacy": 0.6}
PENDING_TTL_DAYS = 30      # a lesson nobody reviewed in this long is archived (see writer.expire_pending)
```

- add a `status: str = "active"` keyword to `envelope_props` (after `confidence`) and use it: replace `"status": "active"` in the returned dict with `"status": status`;
- add after `utc_now`:

```python
def pending_expired(created_at: str, *, now: Optional[datetime] = None,
                    days: int = PENDING_TTL_DAYS) -> bool:
    """True when a pending record created at `created_at` (ISO 8601) is older than `days`.
    An empty or unparseable time is never expired: a record is not archived on a guess."""
    try:
        created = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - created > timedelta(days=days)
```

`src/agent_memory/writer.py`:
- extend the `.record` import with `PENDING_TTL_DAYS, SOURCE_CONFIDENCE, pending_expired` and `from datetime import datetime`;
- in `add`, add the keyword `status: str = "active"` after `source`; right after the `kind not in KINDS` check add:

```python
        if status not in ("active", "pending"):
            raise ValueError(f"status must be 'active' or 'pending', got {status!r}")
        pending = status == "pending"
```

- change `existing = self._active_with_text(kind, norm) if norm else None` to `... if norm and not pending else None`;
- change `similar = self._similar(kind, text) if node_id is None else []` to `... if node_id is None and not pending else []`;
- pass `status=status` to `envelope_props(...)`;
- change `if norm: self._by_text[(kind, norm)] = ref` to `if norm and not pending: ...`;
- add these methods after `feedback` (before `# -- helpers`):

```python
    # -- lessons waiting for review -----------------------------------------

    def find_text(self, kind: str, fields: Dict[str, Any]) -> Optional[Tuple[NodeRef, str]]:
        """(ref, "active" | "pending") of a record of `kind` whose text equals the text these
        fields build, preferring an active one. An answered question or resolved blocker is
        history, not a match (the same rule as a merge)."""
        props = {k: ("" if v is None else v) for k, v in fields.items()}
        norm = normalize_text(build_text(kind, props))
        if not norm:
            return None
        pending_hit: Optional[Tuple[NodeRef, str]] = None
        for node in self.graph.nodes(kind):
            rec = MemoryRecord.from_node(node)
            if rec.status not in ("active", "pending") or normalize_text(rec.text) != norm:
                continue
            if rec.status == "active":
                if is_closed(kind, rec.fields):
                    continue
                return rec.ref, "active"
            pending_hit = pending_hit or (rec.ref, "pending")
        return pending_hit

    def pending(self) -> List[MemoryRecord]:
        """Every record waiting for review, oldest first."""
        out = [MemoryRecord.from_node(node) for kind in KINDS for node in self.graph.nodes(kind)]
        out = [rec for rec in out if rec.status == "pending"]
        out.sort(key=lambda rec: (rec.created_at, str(rec.id)))
        return out

    @transactional
    def accept(self, ref: NodeRef) -> None:
        """A person confirmed this pending record: it becomes an active, reviewed note."""
        rec = MemoryRecord.from_node(self.graph.get_node(*ref))
        if rec.status != "pending":
            raise ValueError(f"{ref[1]!r} is {rec.status}, not pending")
        hit = self.find_text(ref[0], rec.fields)
        if hit is not None and hit[1] == "active" and hit[0] != ref:
            raise ValueError(f"an active note with the same text already exists ({hit[0][1]}); "
                             "reject this lesson instead")
        self.graph.update_node(ref[0], ref[1], status="active", source="reviewed",
                               confidence=SOURCE_CONFIDENCE["reviewed"], invalid_at="",
                               updated_at=utc_now())

    @transactional
    def expire_pending(self, now: Optional[datetime] = None, days: int = PENDING_TTL_DAYS) -> int:
        """Archive pending records older than `days`. Returns how many."""
        count = 0
        for rec in self.pending():
            if pending_expired(rec.created_at, now=now, days=days):
                self._set_status(rec.ref, "archived")
                count += 1
        return count
```

`src/agent_memory/memory.py`:
- add `from datetime import datetime`, `Tuple` to the typing import, and `NodeRef` to `from .record import KINDS, MemoryRecord` (`NodeRef`);
- add after `verify`:

```python
    # =========================================================================
    # Lessons waiting for review
    # =========================================================================

    def pending_records(self) -> List[MemoryRecord]:
        """Records a person has not accepted yet (status "pending"), oldest first. They are
        never recalled, checked or carried."""
        self._sync()
        return self.writer.pending()

    def find_duplicate(self, kind: str, fields: Dict[str, Any]) -> Optional[Tuple[NodeRef, str]]:
        """(ref, "active" | "pending") of a record with the text these fields build, or None."""
        self._sync()
        return self.writer.find_text(kind, fields)

    @transactional
    def accept(self, memory_id) -> None:
        """Promote a pending record to an active, `reviewed` note (a person's decision)."""
        self.writer.accept(self.writer.find(memory_id))

    @transactional
    def expire_pending(self, now: Optional[datetime] = None) -> int:
        """Archive pending records older than PENDING_TTL_DAYS; returns how many."""
        return self.writer.expire_pending(now)

    @transactional
    def capture(self, lessons: List[Dict[str, Any]], *, max_pending: int) -> Dict[str, Any]:
        """Store lessons as pending records, atomically across processes: sweep expired ones,
        count what is waiting, then add each lesson unless it duplicates an active or pending
        note or the queue is full. Each lesson is `{"kind", "fields", "anchors", "evidence"}`,
        already validated by the caller. Nothing here ever changes an existing record."""
        swept = self.writer.expire_pending()
        total = len(self.writer.pending())
        room = max_pending - total
        if room <= 0:
            return {"captured": [], "skipped": [], "full": True, "swept": swept, "pending": total}
        captured: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for index, item in enumerate(lessons):
            hit = self.writer.find_text(item["kind"], item["fields"])
            if hit is not None:
                skipped.append({"index": index, "code": "duplicate",
                                "of": hit[0][1] if hit[1] == "active" else None,
                                "of_status": hit[1]})
            elif room <= 0:
                skipped.append({"index": index, "code": "queue_full", "of": None,
                                "of_status": None})
            else:
                result = self.writer.add(item["kind"], item["fields"], anchors=item["anchors"],
                                         evidence=item["evidence"], source="extracted",
                                         status="pending")
                captured.append({"index": index, "id": result.id})
                room -= 1
        return {"captured": captured, "skipped": skipped, "full": False, "swept": swept,
                "pending": total + len(captured)}
```

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `python -m pytest tests/test_pending_engine.py -q` → all pass.
Run: `python -m pytest tests -q` → 793 + new tests passed, 6 skipped, 8 deselected, no failures.
Run: `PYTHONPATH=src python -m tests.eval.run` → recall 1.00, 0 leaks, abstention 1.00 (unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/record.py src/agent_memory/writer.py src/agent_memory/memory.py tests/test_pending_engine.py
git commit -m "feat: pending status, reviewed source and atomic lesson capture in the engine"
```

---

### Task 2: Service — the capture gate, `session_end(lessons=)`, counts

**Files:**
- Modify: `src/agent_memory/service/errors.py`, `src/agent_memory/service/service.py`, `tests/test_read_only_reads.py` (add the new write method to its list of refusing methods, near the `"session_start": lambda: ...` entry at about line 249)
- Test: `tests/test_service_capture.py` (create)

**Interfaces:**
- Consumes (Task 1): `AgentMemory.capture`, `pending_records`, `record.pending_expired`.
- Produces (used by Tasks 3-6):
  - `MemoryService.capture(lessons) -> {"captured": [escaped ids], "skipped": [{"index": int, "reason": str}], "pending": int}`; raises `ServiceError("queue_full")` when there is no room, `too_large` for more than 10, `invalid_argument` for a non-list or empty list
  - `MemoryService.session_end(summary="", completion=1.0, lessons=None)`; result gains `"capture"` (the capture result, or `{"error": {"code", "message"}}`) only when lessons were passed
  - `MemoryService.status()` gains `"pending"` and `"pending_expired"`; `session_start()` results gain `"pending_review"`
  - module constants `MAX_LESSONS_PER_CALL`, `MAX_PENDING` in `service/service.py`

A lesson is `{"kind", "fields", "evidence", "files"?, "symbols"?}`; any other key skips it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_service_capture.py`:

```python
import pytest

from agent_memory import anchors as anchors_mod
from agent_memory.service import CODES, MemoryService, ServiceError, resolve_config
from agent_memory.service import service as service_module

TEXT = "the zorblax queue drops items over 64KB"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def svc(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="tester", env={}))
    yield service
    service.close()


def good(text=TEXT, **change):
    lesson = {"kind": "quirk", "fields": {"description": text}, "evidence": "seen in a test run",
              "files": ["src/cache.py"]}
    lesson.update(change)
    return lesson


def code_of(excinfo):
    return excinfo.value.code


def test_queue_full_is_a_service_error_code():
    assert "queue_full" in CODES


# -- the happy path ----------------------------------------------------------

def test_a_lesson_is_captured_as_a_pending_record_nobody_recalls(svc):
    out = svc.capture([good()])
    assert set(out) == {"captured", "skipped", "pending"}
    assert len(out["captured"]) == 1 and out["skipped"] == [] and out["pending"] == 1
    record = svc.memory.pending_records()[0]
    assert (record.source, record.confidence, record.agent, record.status) == (
        "extracted", 0.5, "tester", "pending")
    assert record.evidence == "seen in a test run"
    assert anchors_mod.anchor_paths(svc.memory.graph, record.ref) == ["src/cache.py"]
    assert svc.recall("zorblax queue drops items")["items"] == []
    assert svc.check("zorblax queue drops items over 64KB")["items"] == []


def test_a_lesson_with_only_symbols_is_captured_and_keeps_them_in_the_evidence(svc):
    out = svc.capture([good(files=[], symbols=["CacheClient.get", "evict"])])
    assert len(out["captured"]) == 1
    evidence = svc.memory.pending_records()[0].evidence
    assert evidence == "seen in a test run; symbols: CacheClient.get, evict"


# -- the gate: one test per rule --------------------------------------------

GATE = [
    ("no evidence", {"evidence": ""}, "evidence"),
    ("evidence not text", {"evidence": 5}, "evidence"),
    ("no anchor", {"files": [], "symbols": []}, "file or symbol"),
    ("kind agents cannot write", {"kind": "session_decision"}, "unknown_kind"),
    ("kind not text", {"kind": ["quirk"]}, "unknown_kind"),
    ("file outside the repo", {"files": ["../outside.py"]}, "path_outside_repo"),
    ("field too long", {"fields": {"description": "x" * 2001}}, "too_large"),
    ("required field missing", {"fields": {"note": "x"}}, "needs"),
    ("priority key", {"priority": "must"}, "unknown lesson key"),
    ("confidence key", {"confidence": 0.9}, "unknown lesson key"),
]


@pytest.mark.parametrize("label,change,expect", GATE, ids=[g[0] for g in GATE])
def test_each_gate_rule_skips_the_lesson_and_names_the_reason(svc, label, change, expect):
    out = svc.capture([good(**change)])
    assert out["captured"] == [] and out["skipped"][0]["index"] == 0
    assert expect in out["skipped"][0]["reason"]
    assert svc.status()["pending"] == 0


def test_a_lesson_that_is_not_an_object_is_skipped(svc):
    out = svc.capture(["just text", good()])
    assert len(out["captured"]) == 1 and out["skipped"][0]["index"] == 0


def test_skip_reasons_never_carry_markup(svc):
    out = svc.capture([good(**{"<i>x": 1})])
    assert "<" not in out["skipped"][0]["reason"]


def test_a_bad_lesson_does_not_lose_the_good_ones(svc):
    out = svc.capture([good(evidence=""), good("another real lesson")])
    assert len(out["captured"]) == 1
    assert [s["index"] for s in out["skipped"]] == [0]
    assert svc.status()["pending"] == 1


def test_call_size_rules(svc):
    with pytest.raises(ServiceError) as too_many:
        svc.capture([good(f"lesson {i}") for i in range(11)])
    assert code_of(too_many) == "too_large"
    for bad in ([], "text", None, {"kind": "quirk"}):
        with pytest.raises(ServiceError) as err:
            svc.capture(bad)
        assert code_of(err) == "invalid_argument"
    assert svc.status()["pending"] == 0


# -- duplicates --------------------------------------------------------------

def test_a_duplicate_of_an_active_note_is_skipped_and_the_note_is_untouched(svc):
    first = svc.remember("quirk", {"description": TEXT})
    node = svc.memory.graph.get_node("quirk", first["id"]).properties
    before = (node["confidence"], node["evidence"])
    out = svc.capture([good()])
    assert out["captured"] == [] and "duplicate" in out["skipped"][0]["reason"]
    assert "active note" in out["skipped"][0]["reason"]
    node = svc.memory.graph.get_node("quirk", first["id"]).properties
    assert (node["confidence"], node["evidence"]) == before


def test_the_same_lesson_twice_gives_one_pending_record(svc):
    svc.capture([good()])
    again = svc.capture([good(), good("a different lesson")])
    assert len(again["captured"]) == 1
    assert "already waiting" in again["skipped"][0]["reason"]
    assert svc.status()["pending"] == 2


# -- queue limit -------------------------------------------------------------

def test_a_full_queue_raises_queue_full_and_stores_nothing(svc, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_PENDING", 2)
    svc.capture([good("one lesson"), good("two lesson")])
    with pytest.raises(ServiceError) as err:
        svc.capture([good("three lesson")])
    assert code_of(err) == "queue_full"
    assert svc.status()["pending"] == 2


def test_partial_room_stores_what_fits_and_skips_the_rest_as_queue_full(svc, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_PENDING", 2)
    out = svc.capture([good("one lesson"), good("two lesson"), good("three lesson")])
    assert len(out["captured"]) == 2
    assert out["skipped"][0]["index"] == 2 and "queue_full" in out["skipped"][0]["reason"]


# -- read-only ---------------------------------------------------------------

def test_a_read_only_service_refuses_capture(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}, read_only=True))
    with pytest.raises(ServiceError) as err:
        service.capture([good()])
    assert code_of(err) == "invalid_argument"
    service.close()


# -- session end -------------------------------------------------------------

def test_session_end_captures_lessons_stamps_the_session_and_still_ends(svc):
    sid = svc.session_start("a task")["session_id"]
    out = svc.session_end("done", lessons=[good(), good(evidence="")])
    assert out["ok"] is True
    assert len(out["capture"]["captured"]) == 1 and out["capture"]["skipped"][0]["index"] == 1
    assert svc.memory.pending_records()[0].session_id == sid
    with pytest.raises(ServiceError) as err:
        svc.session_end()
    assert code_of(err) == "no_active_session"


def test_session_end_reports_a_full_queue_in_the_result_not_as_an_error(svc, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_PENDING", 1)
    svc.capture([good("filler lesson")])
    svc.session_start("a task")
    out = svc.session_end(lessons=[good()])
    assert out["ok"] is True and out["capture"]["error"]["code"] == "queue_full"


def test_session_end_without_lessons_has_no_capture_key(svc):
    svc.session_start("a task")
    assert "capture" not in svc.session_end("done")
    svc.session_start("again")
    assert "capture" not in svc.session_end("done", lessons=[])


def test_session_end_reports_malformed_lessons_in_the_result(svc):
    svc.session_start("a task")
    out = svc.session_end(lessons="not a list")
    assert out["ok"] is True and out["capture"]["error"]["code"] == "invalid_argument"


def test_capture_works_with_no_active_session(svc):
    svc.capture([good()])
    assert svc.memory.pending_records()[0].session_id == ""


# -- counts ------------------------------------------------------------------

def test_status_and_session_start_report_the_pending_count(svc):
    assert svc.status()["pending"] == 0 and svc.status()["pending_expired"] == 0
    svc.capture([good()])
    status = svc.status()
    assert status["pending"] == 1 and status["total_active"] == 0
    assert svc.session_start("t")["pending_review"] == 1
    assert svc.session_start("t")["pending_review"] == 1           # already active: same count


def test_an_expired_lesson_is_counted_apart_until_it_is_swept(svc):
    out = svc.capture([good()])
    ident = out["captured"][0]
    svc.memory.graph.update_node("quirk", ident, created_at="2020-01-01T00:00:00+00:00")
    status = svc.status()
    assert status["pending"] == 0 and status["pending_expired"] == 1
    svc.capture([good("a fresh lesson")])                          # the sweep
    assert svc.status()["pending_expired"] == 0
```

Also in `tests/test_read_only_reads.py`, in the dict of write calls that must refuse (the one containing `"session_start": lambda: svc.session_start("a task")`), add the entry `"capture": lambda: svc.capture([]),` — read the surrounding test first and follow its shape.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_service_capture.py -q`
Expected: FAIL (`AttributeError: 'MemoryService' object has no attribute 'capture'`).

- [ ] **Step 3: Implement**

`src/agent_memory/service/errors.py`: add `"queue_full"` to `CODES` (after `"too_large"`).

`src/agent_memory/service/service.py`:
- add `import logging` and `Tuple` to the typing import; extend the record import with `pending_expired`; extend the shaping import with `escape_id`;
- after the `_KEY`/`_CONTROL` constants add:

```python
MAX_LESSONS_PER_CALL = 10
MAX_PENDING = 100
_LESSON_KEYS = frozenset(("kind", "fields", "evidence", "files", "symbols"))
_log = logging.getLogger(__name__)
```

- add this block after `retract`/`verify` (before `# -- sessions`), as a new section:

```python
    # -- lessons (pending until a person accepts) --------------------------------

    def _lesson(self, lesson: Any) -> Dict[str, Any]:
        """One lesson, validated by the same rules as `remember`, plus: evidence is required
        and at least one file or symbol. Raises ServiceError; the caller skips the lesson."""
        if not isinstance(lesson, dict):
            raise _bad("each lesson must be an object")
        extra = sorted(str(key) for key in lesson if key not in _LESSON_KEYS)
        if extra:
            raise _bad(f"unknown lesson key(s): {', '.join(extra)}; "
                       "allowed: kind, fields, evidence, files, symbols")
        clean = self._fields(lesson.get("kind"), lesson.get("fields"))
        evidence = self._text("evidence", lesson.get("evidence"), MAX_FIELD_CHARS)
        anchors = self._files(lesson.get("files"))
        symbols = self._symbols(lesson.get("symbols"))
        if not anchors and not symbols:
            raise _bad("a lesson needs at least one file or symbol it is about")
        if symbols:
            evidence = f"{evidence}; symbols: {', '.join(symbols)}"
        return {"kind": lesson["kind"], "fields": clean, "anchors": anchors, "evidence": evidence}

    @staticmethod
    def _skip_reason(item: Dict[str, Any]) -> str:
        if item["code"] == "queue_full":
            return "queue_full: the review queue filled up; a person must review it first"
        if item["of_status"] == "active":
            return (f"duplicate: an active note with this text already exists "
                    f"({escape_id(str(item['of']))})")
        return "duplicate: this lesson is already waiting for review"

    def capture(self, lessons: Any) -> Dict[str, Any]:
        """Leave lessons for a person to review. They are stored pending: never recalled,
        checked or carried until someone accepts them with `amem review accept`. Each lesson
        that fails the gate is skipped with a reason; the others are stored."""
        with self._lock:
            self._refuse_if_read_only("capture lessons")
            self._ensure_open()
            if not isinstance(lessons, (list, tuple)) or not lessons:
                raise _bad("lessons must be a non-empty list")
            if len(lessons) > MAX_LESSONS_PER_CALL:
                raise ServiceError("too_large",
                                   f"at most {MAX_LESSONS_PER_CALL} lessons per call")
            prepared: List[Any] = []
            skipped: List[Dict[str, Any]] = []
            for index, lesson in enumerate(lessons):
                try:
                    prepared.append((index, self._lesson(lesson)))
                except ServiceError as exc:
                    skipped.append({"index": index,
                                    "reason": neutralize(f"{exc.code}: {exc.message}")})
            captured: List[str] = []
            pending = None
            if prepared:
                outcome = self._call(lambda: self.memory.capture(
                    [item for _, item in prepared], max_pending=MAX_PENDING))
                if outcome["full"]:
                    raise ServiceError(
                        "queue_full", f"{MAX_PENDING} lessons are already waiting for review; "
                        "a person must accept or reject some (amem review list) before more "
                        "can be captured")
                captured = [escape_id(str(c["id"])) for c in outcome["captured"]]
                for item in outcome["skipped"]:
                    skipped.append({"index": prepared[item["index"]][0],
                                    "reason": neutralize(self._skip_reason(item))})
                pending = outcome["pending"]
            if pending is None:
                pending = self._call(self._pending_counts)[0]
            return {"captured": captured, "skipped": sorted(skipped, key=lambda s: s["index"]),
                    "pending": pending}

    def _pending_counts(self) -> Tuple[int, int]:
        """(waiting, expired but not yet swept)."""
        live = expired = 0
        for record in self.memory.pending_records():
            if pending_expired(record.created_at):
                expired += 1
            else:
                live += 1
        return live, expired
```

- `session_start`: compute `pending = self._call(lambda: self._pending_counts()[0])` right after `task = self._text(...)`, and add `"pending_review": pending` to both returned dicts.
- Replace `session_end` with:

```python
    def session_end(self, summary: str = "", completion: float = 1.0,
                    lessons: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        with self._lock:
            self._refuse_if_read_only("end a session")
            self._ensure_open()
            if self._session is None:
                raise ServiceError("no_active_session", "no session is active; start one first")
            if (isinstance(completion, bool) or not isinstance(completion, (int, float))
                    or not 0.0 <= float(completion) <= 1.0):
                raise _bad("completion must be a number between 0 and 1")
            summary = self._text("summary", summary, MAX_FIELD_CHARS, allow_empty=True)
            unresolved = [item_to_dict(i) for i in self._call(self.memory.carried_items)]
            session_id = self._session
            capture = None
            if lessons is not None and not (isinstance(lessons, (list, tuple)) and not lessons):
                # stamped with this session, and never able to stop the session from ending
                try:
                    capture = self.capture(lessons)
                except ServiceError as exc:
                    if exc.code == "internal":
                        _log.error("internal failure capturing lessons: %s", exc.message,
                                   exc_info=exc.__cause__ or exc)
                    capture = {"error": exc.to_dict()}
            if summary:
                self._call(lambda: self.memory.remember(
                    "session_decision", {"decision": summary, "rationale": "end-of-session summary"},
                    source="explicit"))
            self._call(lambda: self.memory.end_session(session_id, float(completion)))
            self._session = None
            self.memory.set_actor(self.config.agent, "")
            result = {"ok": True, "session_id": session_id, "unresolved": unresolved}
            if capture is not None:
                result["capture"] = capture
            return result
```

- `_status`: before the node loop add `pending = expired = 0`; extend the loop body so that after the `if record.status == "active": ...` block there is

```python
                elif record.status == "pending":
                    if pending_expired(record.created_at):
                        expired += 1
                    else:
                        pending += 1
```

and add `"pending": pending, "pending_expired": expired,` to the returned dict (before `"warnings"`).

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `python -m pytest tests/test_service_capture.py -q` → all pass.
Run: `python -m pytest tests -q` → no failures (fix any test that asserts the exact key set of `status` or `session_start` by adding the new keys, nothing else).
Run: `PYTHONPATH=src python -m tests.eval.run --service` → 1.00 / 0 / 1.00 unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/service tests/test_service_capture.py tests/test_read_only_reads.py
git commit -m "feat: capture lessons into a pending queue through the service gate"
```

---

### Task 3: MCP — `memory_capture_lessons`, `lessons` on `memory_session_end`, protocol text

**Files:**
- Modify: `src/agent_memory/mcp_server.py`, `src/agent_memory/protocol.py`, `tests/test_mcp_server.py` (the tool-count assertion), `tests/test_installer_tools.py:161-168` (the length limits), `tests/golden/install/**` (regenerated)
- Test: `tests/test_mcp_capture.py` (create)

**Interfaces:**
- Consumes (Task 2): `MemoryService.capture`, `MemoryService.session_end(..., lessons=)`.
- Produces: tool `memory_capture_lessons(lessons)`; `memory_session_end(summary, completion, lessons)`; `TOOL_NAMES` has 11 names; the MCP block mentions `lessons` and a person.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_capture.py`:

```python
import asyncio
import json

import pytest

pytest.importorskip("mcp")

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from agent_memory.mcp_server import TOOL_NAMES, build_server            # noqa: E402
from agent_memory.protocol import block                                  # noqa: E402
from agent_memory.service import MemoryService, resolve_config           # noqa: E402

TEXT = "the zorblax queue drops items over 64KB"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 90))


def lesson(text=TEXT, **change):
    base = {"kind": "quirk", "fields": {"description": text}, "evidence": "seen in a test run",
            "files": ["src/cache.py"]}
    base.update(change)
    return base


def with_client(repo, scenario):
    service = MemoryService(resolve_config(repo=str(repo), env={}))

    async def go():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            return await scenario(client)

    try:
        return run(go()), service
    finally:
        service.close()


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    return result.isError, result.content[0].text


def test_the_surface_is_the_eleven_tools_and_none_of_them_can_accept(repo):
    async def scenario(client):
        return (await client.list_tools()).tools

    tools, _ = with_client(repo, scenario)
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES) and len(TOOL_NAMES) == 11
    assert "memory_capture_lessons" in TOOL_NAMES
    for name in TOOL_NAMES:
        assert not any(word in name for word in ("review", "accept", "reject", "promote"))
    assert all(len(t.description) <= 400 for t in tools)
    assert sum(len(t.description) for t in tools) <= 3000


def test_lessons_are_a_required_list_on_the_capture_tool_and_optional_on_session_end(repo):
    async def scenario(client):
        return {t.name: t for t in (await client.list_tools()).tools}

    tools, _ = with_client(repo, scenario)
    assert tools["memory_capture_lessons"].inputSchema["required"] == ["lessons"]
    end = tools["memory_session_end"].inputSchema
    assert "lessons" in end["properties"] and "lessons" not in end.get("required", [])
    assert "lessons" in tools["memory_session_end"].description
    assert "person" in tools["memory_capture_lessons"].description


def test_a_captured_lesson_is_not_recalled(repo):
    async def scenario(client):
        err, text = await call(client, "memory_capture_lessons", lessons=[lesson()])
        assert not err, text
        captured = json.loads(text)
        err, recalled = await call(client, "memory_recall", task="zorblax queue drops items")
        assert not err, recalled
        return captured, json.loads(recalled)

    (captured, recalled), _ = with_client(repo, scenario)
    assert len(captured["captured"]) == 1 and captured["pending"] == 1
    assert recalled["items"] == []


def test_session_end_takes_lessons_and_a_json_looking_evidence_survives(repo):
    evidence = '{"error": "StripeSignatureError: no signatures found"}'

    async def scenario(client):
        await call(client, "memory_session_start", task="t")
        err, text = await call(client, "memory_session_end", summary="done",
                               lessons=[lesson(evidence=evidence)])
        assert not err, text
        return json.loads(text)

    out, service = with_client(repo, scenario)
    assert out["ok"] is True and len(out["capture"]["captured"]) == 1


def test_a_full_queue_is_a_tool_error_with_the_code(repo, monkeypatch):
    from agent_memory.service import service as service_module
    monkeypatch.setattr(service_module, "MAX_PENDING", 1)

    async def scenario(client):
        await call(client, "memory_capture_lessons", lessons=[lesson("filler lesson")])
        return await call(client, "memory_capture_lessons", lessons=[lesson()])

    (is_error, text), _ = with_client(repo, scenario)
    assert is_error and "queue_full" in text


def test_the_mcp_block_tells_agents_about_lessons_and_the_person():
    text = block("mcp")
    assert "lessons" in text and "person" in text
```

In `tests/test_mcp_server.py`, change `len(TOOL_NAMES) == 10` to `len(TOOL_NAMES) == 11` (line ~48) and rename the test if its name says "ten".

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_mcp_capture.py -q`
Expected: FAIL (`memory_capture_lessons` not in `TOOL_NAMES`).

- [ ] **Step 3: Implement**

`src/agent_memory/mcp_server.py`:
- `TOOL_NAMES`: insert `"memory_capture_lessons"` after `"memory_session_end"`.
- Replace the `memory_session_end` tool and add the capture tool after it:

```python
    @app.tool(description="End the current session. Optional one-line summary and optional "
                          "lessons (as in memory_capture_lessons). Returns the items still "
                          "unresolved and, when lessons were given, the capture result.")
    def memory_session_end(ctx: Context, summary: str = "", completion: float = 1.0,
                           lessons: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return run(ctx, service.session_end, summary=summary, completion=completion,
                   lessons=lessons)

    @app.tool(description="Hand over lessons from this session for a person to review. Each "
                          "lesson: {kind, fields, evidence, files, symbols}; kind and fields "
                          "as in memory_remember; evidence and at least one file or symbol "
                          "are required. Lessons are NOT recalled until a person accepts "
                          "them. Up to 10 per call. Returns captured ids and skipped "
                          "lessons with reasons.")
    def memory_capture_lessons(lessons: List[Dict[str, Any]], ctx: Context) -> Dict[str, Any]:
        return run(ctx, service.capture, lessons=lessons)
```

`src/agent_memory/protocol.py`: replace step 5 of `_MCP_STEPS` with

```
5. When you finish call `memory_session_end`; pass `lessons` (each with `evidence` and a file) for what a future agent should know: a person must accept them before anyone recalls them.
```

- Measure: `python -c "from agent_memory.protocol import block; print(len(block('mcp')), len(block('both')))"` (with `PYTHONPATH=src`). In `tests/test_installer_tools.py::test_protocol_blocks_stay_within_their_length_limits` raise the mcp limit from 1200 to the next multiple of 100 above the measured length (about 1400), keeping `splitlines() <= 12`; raise the "both" limit (3000) only if the measured length exceeds it, to the next 100. Put the reason in the commit message: "raise protocol length limit for the lessons clause (3c spec 4.6)".
- Regenerate goldens: `UPDATE_GOLDEN=1 PYTHONPATH=src python -m pytest tests/test_installer_tools.py -q -k golden`, then read `git diff tests/golden` and confirm only the step 5 line changed in each golden that carries the MCP steps.

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `python -m pytest tests/test_mcp_capture.py tests/test_mcp_server.py tests/test_installer_tools.py -q` → pass.
Run: `python -m pytest tests -q` → no failures.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/mcp_server.py src/agent_memory/protocol.py tests
git commit -m "feat: memory_capture_lessons tool, lessons on session end, protocol clause"
```

---

### Task 4: Service — review methods (list, show, accept, reject)

**Files:**
- Modify: `src/agent_memory/service/service.py`, `src/agent_memory/service/shaping.py` (comment only), `tests/test_read_only_reads.py` (add `review_accept` and `review_reject` to the refusing-writes list)
- Test: `tests/test_service_review.py` (create)

**Interfaces:**
- Consumes (Task 1): `AgentMemory.accept`, `expire_pending`, `pending_records`.
- Produces (used by Task 5):
  - `review_list() -> {"pending": [entry], "expired": int, "limit": 100}`; entry = `{"id", "kind", "text", "agent", "session_id", "created_at", "expired"}`
  - `review_show(id) -> entry + {"fields": {name: str}, "evidence", "files": [str], "confidence": float, "source": str}`; `invalid_argument` if not pending, `not_found` if unknown
  - `review_accept(id) -> {"ok": True, "id", "status": "active"}`; `review_reject(id, reason="") -> {"ok": True, "id"}`
  - `list` and `show` work on a read-only service; `accept` and `reject` refuse on one

- [ ] **Step 1: Write the failing tests**

Create `tests/test_service_review.py`:

```python
import pytest

from agent_memory.service import MemoryService, ServiceError, resolve_config

TEXT = "the zorblax queue drops items over 64KB"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def svc(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="tester", env={}))
    yield service
    service.close()


def good(text=TEXT, **change):
    lesson = {"kind": "quirk", "fields": {"description": text}, "evidence": "seen in a test run",
              "files": ["src/cache.py"]}
    lesson.update(change)
    return lesson


def capture_one(svc, text=TEXT, **change):
    return svc.capture([good(text, **change)])["captured"][0]


def code_of(excinfo):
    return excinfo.value.code


# -- list and show -----------------------------------------------------------

def test_list_shows_pending_lessons_oldest_first_with_limit(svc):
    first = capture_one(svc, "first lesson text")
    second = capture_one(svc, "second lesson text")
    out = svc.review_list()
    assert [e["id"] for e in out["pending"]] == [first, second]
    assert out["limit"] == 100 and out["expired"] == 0
    assert set(out["pending"][0]) == {"id", "kind", "text", "agent", "session_id",
                                      "created_at", "expired"}


def test_show_gives_the_whole_lesson(svc):
    ident = capture_one(svc, evidence="the reviewer reads this")
    shown = svc.review_show(ident)
    assert shown["kind"] == "quirk" and shown["agent"] == "tester"
    assert shown["fields"] == {"description": TEXT}
    assert shown["evidence"] == "the reviewer reads this"
    assert shown["files"] == ["src/cache.py"]
    assert shown["source"] == "extracted" and shown["confidence"] == 0.5


def test_show_refuses_an_unknown_id_and_a_record_that_is_not_pending(svc):
    with pytest.raises(ServiceError) as unknown:
        svc.review_show("quirk_nothing_000000")
    assert code_of(unknown) == "not_found"
    active = svc.remember("quirk", {"description": "an active note"})["id"]
    with pytest.raises(ServiceError) as active_err:
        svc.review_show(active)
    assert code_of(active_err) == "invalid_argument"


def test_recorded_text_is_neutralised_in_list_and_show(svc):
    ident = capture_one(svc, "use <b>bold</b> \x1b[31mred\x1b[0m tags", evidence="<i>why</i>\x07")
    listed = svc.review_list()["pending"][0]
    shown = svc.review_show(ident)
    for text in (listed["text"], shown["evidence"], shown["fields"]["description"]):
        assert "<" not in text and "\x1b" not in text and "\x07" not in text


def test_list_and_show_work_on_a_read_only_service(repo, svc):
    ident = capture_one(svc)
    svc.close()
    ro = MemoryService(resolve_config(repo=str(repo), env={}, read_only=True))
    assert [e["id"] for e in ro.review_list()["pending"]] == [ident]
    assert ro.review_show(ident)["kind"] == "quirk"
    ro.close()


def test_an_expired_lesson_is_flagged_in_the_list(svc):
    ident = capture_one(svc)
    svc.memory.graph.update_node("quirk", ident, created_at="2020-01-01T00:00:00+00:00")
    out = svc.review_list()
    assert out["pending"][0]["expired"] is True and out["expired"] == 1


# -- accept ------------------------------------------------------------------

def test_accept_makes_the_lesson_recallable_and_reviewed(svc):
    ident = capture_one(svc)
    assert svc.review_accept(ident) == {"ok": True, "id": ident, "status": "active"}
    checked = svc.check("zorblax queue drops items over 64KB")
    assert [i["id"] for i in checked["items"]] == [ident]
    assert checked["items"][0]["unreviewed"] is False and checked["note"] == ""
    assert svc.review_list()["pending"] == []
    assert svc.status()["total_active"] == 1 and svc.status()["pending"] == 0


def test_accept_twice_is_refused(svc):
    ident = capture_one(svc)
    svc.review_accept(ident)
    with pytest.raises(ServiceError) as err:
        svc.review_accept(ident)
    assert code_of(err) == "invalid_argument"


def test_accept_refuses_when_an_active_note_now_has_the_same_text(svc):
    ident = capture_one(svc)
    svc.remember("quirk", {"description": TEXT})
    with pytest.raises(ServiceError) as err:
        svc.review_accept(ident)
    assert code_of(err) == "invalid_argument" and "same text" in err.value.message
    assert svc.review_show(ident)["kind"] == "quirk"            # still pending


def test_accept_refuses_a_lesson_that_expired_in_the_meantime(svc):
    ident = capture_one(svc)
    svc.memory.graph.update_node("quirk", ident, created_at="2020-01-01T00:00:00+00:00")
    with pytest.raises(ServiceError) as err:
        svc.review_accept(ident)
    assert code_of(err) == "invalid_argument" and "archived" in err.value.message


def test_accept_refuses_a_kind_agents_cannot_write(svc):
    result = svc.memory.remember("session_decision", {"decision": "x", "rationale": "y"},
                                 source="extracted", status="pending")
    with pytest.raises(ServiceError) as err:
        svc.review_accept(str(result.id))
    assert code_of(err) == "invalid_argument"


def test_a_read_only_service_refuses_accept_and_reject(repo, svc):
    ident = capture_one(svc)
    svc.close()
    ro = MemoryService(resolve_config(repo=str(repo), env={}, read_only=True))
    for call in (lambda: ro.review_accept(ident), lambda: ro.review_reject(ident)):
        with pytest.raises(ServiceError) as err:
            call()
        assert code_of(err) == "invalid_argument"
    ro.close()


# -- reject ------------------------------------------------------------------

def test_reject_retracts_the_lesson_and_keeps_the_reason(svc):
    ident = capture_one(svc)
    assert svc.review_reject(ident, "not true") == {"ok": True, "id": ident}
    node = svc.memory.graph.get_node("quirk", ident).properties
    assert node["status"] == "retracted" and "not true" in node["evidence"]
    assert svc.review_list()["pending"] == [] and svc.status()["total_active"] == 0
    assert svc.recall("zorblax queue drops items")["items"] == []


def test_reject_refuses_a_record_that_is_not_pending(svc):
    active = svc.remember("quirk", {"description": "an active note"})["id"]
    with pytest.raises(ServiceError) as err:
        svc.review_reject(active)
    assert code_of(err) == "invalid_argument"
```

Also in `tests/test_read_only_reads.py` add `"review_accept": lambda: svc.review_accept("x")` and `"review_reject": lambda: svc.review_reject("x")` to the refusing-writes list, following its shape.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_service_review.py -q`
Expected: FAIL (`AttributeError: ... 'review_list'`).

- [ ] **Step 3: Implement**

`src/agent_memory/service/service.py`: add `from .. import anchors as anchors_mod` to the imports and add this section after `_pending_counts`:

```python
    # -- review: for the person at a terminal -------------------------------------
    # No MCP tool and no adapter an agent reaches calls these. `amem review` does, and its
    # `accept` insists on an interactive terminal.

    def _review_entry(self, record: MemoryRecord) -> Dict[str, Any]:
        return {"id": escape_id(str(record.id)), "kind": neutralize(str(record.kind)),
                "text": neutralize(record.text), "agent": neutralize(str(record.agent)),
                "session_id": neutralize(str(record.session_id)),
                "created_at": neutralize(str(record.created_at)),
                "expired": pending_expired(record.created_at)}

    def review_list(self) -> Dict[str, Any]:
        with self._lock:
            records = self._call(self.memory.pending_records)
            entries = [self._review_entry(r) for r in records]
            return {"pending": entries, "expired": sum(1 for e in entries if e["expired"]),
                    "limit": MAX_PENDING}

    def _pending_record(self, ident: str) -> MemoryRecord:
        record = self._lookup(ident)
        if record.status != "pending":
            raise _bad(f"{ident!r} is {record.status}, not waiting for review")
        return record

    def review_show(self, id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._pending_record(self._id(id))
            entry = self._review_entry(record)
            entry.update({
                "fields": {neutralize(str(k)): neutralize(str(v))
                           for k, v in record.fields.items() if k != "status"},
                "evidence": neutralize(record.evidence),
                "files": [neutralize(p) for p in
                          self._call(lambda: anchors_mod.anchor_paths(self.memory.graph,
                                                                       record.ref))],
                "confidence": round(float(record.confidence), 2),
                "source": neutralize(str(record.source))})
            return entry

    def review_accept(self, id: str) -> Dict[str, Any]:
        with self._lock:
            self._refuse_if_read_only("accept a lesson")
            ident = self._id(id)
            self._call(self.memory.expire_pending)
            record = self._lookup(ident, agent_kinds_only=True)
            if record.status != "pending":
                raise _bad(f"{ident!r} is {record.status}, not waiting for review")
            self._call(lambda: self.memory.accept(ident))
            return {"ok": True, "id": ident, "status": "active"}

    def review_reject(self, id: str, reason: str = "") -> Dict[str, Any]:
        with self._lock:
            self._refuse_if_read_only("reject a lesson")
            ident = self._id(id)
            reason = self._text("reason", reason, MAX_FIELD_CHARS, allow_empty=True)
            self._call(self.memory.expire_pending)
            record = self._lookup(ident, agent_kinds_only=True)
            if record.status != "pending":
                raise _bad(f"{ident!r} is {record.status}, not waiting for review")
            self._call(lambda: self.memory.retract(ident, reason or "rejected in review"))
            return {"ok": True, "id": ident}
```

`src/agent_memory/service/shaping.py`: extend the comment above `UNREVIEWED_SOURCES` with one line: "A lesson a person accepts is stamped "reviewed" (3c), which is not in this set." No code change.

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `python -m pytest tests/test_service_review.py -q` → pass.
Run: `python -m pytest tests -q` → no failures.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/service tests/test_service_review.py tests/test_read_only_reads.py
git commit -m "feat: review methods for pending lessons in the service"
```

---

### Task 5: CLI — `amem capture`, `amem review`, status and doctor

**Files:**
- Create: `src/agent_memory/cli_review.py`
- Modify: `src/agent_memory/cli.py` (register), `src/agent_memory/cli_agent.py` (`status` lines), `src/agent_memory/doctor.py` (`pending` check), `src/agent_memory/protocol.py` (CLI step 5), `tests/test_installer_tools.py` (limits if needed), `tests/golden/install/**` (regenerated), `tests/test_doctor.py` (any exact check-list assertion)
- Test: `tests/test_cli_review.py` (create)

**Interfaces:**
- Consumes (Task 4): `review_list/show/accept/reject`; (Task 2) `capture`, `status["pending"]`.
- Produces: commands `amem capture [--session ID]` (JSON list on stdin), `amem review list|show ID|accept ID...|reject ID... [--reason]`. `accept` requires stdin and stdout to be a terminal (`cli_review._interactive()` is the single seam tests patch), asks the person to type `accept` per lesson, has no `--yes` and no `--json`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_review.py`:

```python
import json

import pytest
from click.testing import CliRunner

from agent_memory import cli_review
from agent_memory.cli import main

TEXT = "the zorblax queue drops items over 64KB"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def amem(repo):
    runner = CliRunner()

    def invoke(*args, input=None, expect=0, json_out=False):
        argv = [*args, "--repo", str(repo)] + (["--json"] if json_out else [])
        result = runner.invoke(main, argv, input=input)
        assert result.exit_code == expect, result.output
        return json.loads(result.output) if json_out else result.output

    return invoke


def lessons_json(*texts, **change):
    out = []
    for text in texts:
        item = {"kind": "quirk", "fields": {"description": text},
                "evidence": "seen in a test run", "files": ["src/cache.py"]}
        item.update(change)
        out.append(item)
    return json.dumps(out)


def capture(amem, *texts, **change):
    return amem("capture", input=lessons_json(*texts, **change), json_out=True)


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(cli_review, "_interactive", lambda: True)


# -- capture -----------------------------------------------------------------

def test_capture_reads_a_json_list_from_stdin(amem):
    out = capture(amem, TEXT)
    assert len(out["captured"]) == 1 and out["pending"] == 1


def test_capture_rejects_input_that_is_not_a_json_list(amem):
    out = amem("capture", input="not json", expect=2, json_out=True)
    assert out["error"]["code"] == "invalid_argument"
    out = amem("capture", input='{"kind": "quirk"}', expect=2, json_out=True)
    assert out["error"]["code"] == "invalid_argument"


def test_capture_refuses_oversized_input_without_reading_it_all(amem):
    out = amem("capture", input="[" + "0," * 600_000 + "0]", expect=2, json_out=True)
    assert out["error"]["code"] == "too_large"


def test_capture_plain_output_lists_captured_and_skipped(amem):
    both = json.loads(lessons_json(TEXT)) + [{"kind": "x"}]
    text = amem("capture", input=json.dumps(both))
    assert "captured 1" in text and "skipped #1" in text and "waiting for review" in text


# -- list and show -----------------------------------------------------------

def test_list_and_show(amem):
    ident = capture(amem, TEXT)["captured"][0]
    listing = amem("review", "list")
    assert ident in listing and "zorblax" in listing and "waiting for review" in listing
    shown = amem("review", "show", ident)
    assert "Agent-written" in shown and "evidence: seen in a test run" in shown
    assert "src/cache.py" in shown
    assert amem("review", "list", json_out=True)["pending"][0]["id"] == ident


def test_show_and_list_never_print_terminal_escapes_or_markup(amem):
    ident = capture(amem, "see <b>x</b> \x1b]0;pwned\x07 end")["captured"][0]
    for text in (amem("review", "list"), amem("review", "show", ident)):
        assert "\x1b" not in text and "\x07" not in text and "<b>" not in text


def test_review_list_on_a_missing_store_creates_nothing(amem, repo):
    assert "no lessons" in amem("review", "list")
    assert not (repo / ".agent-memory").exists()


# -- accept ------------------------------------------------------------------

def test_accept_needs_a_real_terminal(amem):
    ident = capture(amem, TEXT)["captured"][0]
    result = amem("review", "accept", ident, input="accept\n", expect=2)
    assert "terminal" in result
    assert amem("review", "list", json_out=True)["pending"][0]["id"] == ident
    assert amem("guard", "zorblax queue drops items over 64KB", json_out=True)["items"] == []


def test_accept_has_no_bypass_flag(amem):
    ident = capture(amem, TEXT)["captured"][0]
    for flag in ("--yes", "-y", "--force", "--no-input"):
        result = CliRunner().invoke(main, ["review", "accept", flag, ident])
        assert result.exit_code == 2 and "No such option" in result.output


def test_accept_promotes_after_the_person_types_accept(amem, terminal):
    ident = capture(amem, TEXT)["captured"][0]
    out = amem("review", "accept", ident, input="accept\n")
    assert "accepted" in out and ident in out
    guard = amem("guard", "zorblax queue drops items over 64KB", json_out=True)
    assert [i["id"] for i in guard["items"]] == [ident]
    assert guard["items"][0]["unreviewed"] is False


def test_anything_but_accept_skips_the_lesson(amem, terminal):
    ident = capture(amem, TEXT)["captured"][0]
    for answer in ("y\n", "\n", "no\n"):
        out = amem("review", "accept", ident, input=answer)
        assert "skipped" in out
    assert amem("review", "list", json_out=True)["pending"][0]["id"] == ident


def test_accept_handles_several_ids_one_at_a_time(amem, terminal):
    first, second = capture(amem, "first lesson text", "second lesson text")["captured"]
    out = amem("review", "accept", first, second, input="accept\nno\n")
    assert out.count("accepted") == 1 and out.count("skipped") == 1


def test_accept_reports_a_bad_id_and_exits_2(amem, terminal):
    out = amem("review", "accept", "quirk_nothing_000000", input="accept\n", expect=2)
    assert "not_found" in out


def test_accept_has_no_json_output(amem, terminal, repo):
    ident = capture(amem, TEXT)["captured"][0]
    result = CliRunner().invoke(main, ["review", "accept", ident, "--json", "--repo", str(repo)],
                                input="accept
")
    assert result.exit_code == 2 and "no --json" in result.output
    assert amem("review", "list", json_out=True)["pending"][0]["id"] == ident


# -- reject ------------------------------------------------------------------

def test_reject_needs_no_terminal(amem):
    ident = capture(amem, TEXT)["captured"][0]
    out = amem("review", "reject", ident, "--reason", "not true")
    assert "rejected" in out
    assert amem("review", "list", json_out=True)["pending"] == []
    assert amem("guard", "zorblax queue drops items over 64KB", json_out=True)["items"] == []


def test_reject_reports_a_bad_id_and_exits_2(amem):
    out = amem("review", "reject", "quirk_nothing_000000", expect=2)
    assert "not_found" in out


# -- status and doctor -------------------------------------------------------

def test_status_and_doctor_report_the_waiting_lessons(amem, repo):
    capture(amem, TEXT)
    assert "lessons waiting for review: 1" in amem("status")
    assert amem("status", json_out=True)["pending"] == 1
    doctor = CliRunner().invoke(main, ["doctor", "--json", "--repo", str(repo)])
    checks = {c["check"]: c for c in json.loads(doctor.output)["checks"]}
    assert checks["pending"]["ok"] and "1 lesson" in checks["pending"]["detail"]


def test_the_cli_protocol_block_mentions_capture():
    from agent_memory.protocol import block
    assert "amem capture" in block("cli") and "amem capture" in block("both")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_cli_review.py -q`
Expected: FAIL (`ImportError: cannot import name 'cli_review'`).

- [ ] **Step 3: Implement**

Create `src/agent_memory/cli_review.py`:

```python
"""`amem capture` (agents leave lessons) and `amem review` (a person reads, accepts, rejects).

`accept` is the only way a lesson becomes a recallable note, and it insists on an interactive
terminal with no flag around it. That is a strong barrier, not proof: a program that drives a
pseudo-terminal could pass it (3c spec, section 4.4)."""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

import click

from .cli_agent import _emit, _fail, _printable, run_service, service_options
from .service import ServiceError

# the largest request the service could accept (10 lessons of 12 fields of 2000 characters in
# 4-byte UTF-8) plus room for the JSON syntax; a bigger pipe is refused after this many bytes
MAX_CAPTURE_BYTES = 1_000_000


def _interactive() -> bool:
    """True only when stdin and stdout are both a terminal. The one seam tests replace."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _read_lessons() -> List[Any]:
    data = click.get_binary_stream("stdin").read(MAX_CAPTURE_BYTES + 1)
    if len(data) > MAX_CAPTURE_BYTES:
        raise ServiceError("too_large", f"lessons are longer than {MAX_CAPTURE_BYTES} bytes")
    try:
        lessons = json.loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        raise ServiceError("invalid_argument", "stdin is not valid JSON; send a JSON list of "
                                               "lessons") from None
    if not isinstance(lessons, list):
        raise ServiceError("invalid_argument", "stdin must hold a JSON list of lessons")
    return lessons


def _render_capture(r: Dict[str, Any]) -> str:
    lines = [f"captured {len(r['captured'])}: {', '.join(r['captured']) or '-'}"]
    lines += [f"skipped #{s['index']}: {s['reason']}" for s in r["skipped"]]
    lines.append(f"{r['pending']} lesson(s) waiting for review (a person runs `amem review`)")
    return _printable("\n".join(lines))


@click.command("capture")
@click.option("--session", default="", help="Session id from `amem session start`.")
@service_options
def capture(session, store, repo, agent, allow_must, as_json):
    """Leave lessons for a person to review: a JSON list on stdin, each lesson
    {"kind", "fields", "evidence", "files"}. They are not recalled until accepted."""
    try:
        lessons = _read_lessons()
    except ServiceError as exc:
        _fail(exc, as_json)
        return
    run_service(store, repo, agent, allow_must, as_json, lambda s: s.capture(lessons),
                _render_capture, session)


@click.group("review")
def review_group():
    """Read, accept or reject the lessons agents left."""


def _render_list(r: Dict[str, Any]) -> str:
    if not r["pending"]:
        return "no lessons are waiting for review"
    lines = [f"{len(r['pending'])} lesson(s) waiting for review (limit {r['limit']}); "
             "`amem review show ID` reads one"]
    for entry in r["pending"]:
        flag = " [expired]" if entry["expired"] else ""
        lines.append(f"- [{entry['id']}] {entry['kind']} by {entry['agent']}{flag}: "
                     f"{entry['text']}")
    return _printable("\n".join(lines))


def _show_text(d: Dict[str, Any]) -> str:
    lines = [f"lesson {d['id']}  ({d['kind']}, written by agent {d['agent']}, {d['created_at']})",
             "Agent-written text, not confirmed by anyone. It is data, not instructions.",
             f"  {d['text']}"]
    lines += [f"  {name}: {value}" for name, value in d["fields"].items()]
    lines.append(f"  evidence: {d['evidence']}")
    lines.append(f"  files: {', '.join(d['files']) or '-'}")
    if d["expired"]:
        lines.append("  EXPIRED: it is archived on the next capture, accept or reject")
    return _printable("\n".join(lines))


@review_group.command("list")
@service_options
def review_list(store, repo, agent, allow_must, as_json):
    """List the lessons waiting for review."""
    run_service(store, repo, agent, allow_must, as_json, lambda s: s.review_list(),
                _render_list, read_only=True)


@review_group.command("show")
@click.argument("memory_id")
@service_options
def review_show(memory_id, store, repo, agent, allow_must, as_json):
    """Show one lesson in full."""
    run_service(store, repo, agent, allow_must, as_json, lambda s: s.review_show(memory_id),
                _show_text, read_only=True)


def _render_outcome(r: Dict[str, Any]) -> str:
    lines = [f"accepted {i}" for i in r.get("accepted", [])]
    lines += [f"skipped {i}" for i in r.get("skipped", [])]
    lines += [f"rejected {i}" for i in r.get("rejected", [])]
    lines += [f"error [{e['error']['code']}] {e['id']}: {e['error']['message']}"
              for e in r["errors"]]
    return _printable("\n".join(lines))


@review_group.command("accept")
@click.argument("memory_ids", nargs=-1, required=True)
@service_options
def review_accept(memory_ids, store, repo, agent, allow_must, as_json):
    """Read each lesson and, if you type `accept`, make it a recallable note.

    Needs an interactive terminal. There is no flag to skip the question."""
    if as_json:
        _fail(ServiceError("invalid_argument", "accept is interactive and has no --json output"),
              False)
    if not _interactive():
        _fail(ServiceError("invalid_argument",
                           "accepting a lesson needs an interactive terminal: run `amem review "
                           "accept` yourself in a terminal"), False)
    outcome: Dict[str, List[Any]] = {"accepted": [], "skipped": [], "errors": []}

    def action(service) -> Dict[str, Any]:
        for ident in memory_ids:
            try:
                _emit(_show_text(service.review_show(ident)))
                answer = click.prompt("Type 'accept' to make this a recallable note "
                                      "(anything else skips it)", default="",
                                      show_default=False)
                if answer.strip().lower() == "accept":
                    service.review_accept(ident)
                    outcome["accepted"].append(ident)
                else:
                    outcome["skipped"].append(ident)
            except ServiceError as exc:
                outcome["errors"].append({"id": ident, "error": exc.to_dict()})
        return outcome

    run_service(store, repo, agent, allow_must, False, action, _render_outcome)
    if outcome["errors"]:
        sys.exit(2)


@review_group.command("reject")
@click.argument("memory_ids", nargs=-1, required=True)
@click.option("--reason", default="", help="Why it is wrong or not wanted (kept with the record).")
@service_options
def review_reject(memory_ids, reason, store, repo, agent, allow_must, as_json):
    """Reject lessons: they are kept for the record and never recalled."""
    outcome: Dict[str, List[Any]] = {"rejected": [], "errors": []}

    def action(service) -> Dict[str, Any]:
        for ident in memory_ids:
            try:
                service.review_reject(ident, reason)
                outcome["rejected"].append(ident)
            except ServiceError as exc:
                outcome["errors"].append({"id": ident, "error": exc.to_dict()})
        return outcome

    run_service(store, repo, agent, allow_must, as_json, action, _render_outcome)
    if outcome["errors"]:
        sys.exit(2)


COMMANDS = [capture, review_group]


def register(group: click.Group) -> None:
    for command in COMMANDS:
        group.add_command(command)
```

`src/agent_memory/cli.py`: at the end where `cli_agent.register(main)` is, add `from . import cli_review` next to the existing `cli_agent` import and `cli_review.register(main)` after it (follow how `cli_agent` is imported there).

`src/agent_memory/cli_agent.py`, in `status`'s `render`, after the `active notes` line add:

```python
        if r.get("pending"):
            lines.append(f"lessons waiting for review: {r['pending']} (amem review list)")
        if r.get("pending_expired"):
            lines.append(f"expired lessons not yet swept: {r['pending_expired']}")
```

`src/agent_memory/doctor.py`, in `_open_checks` after the `journal` check append:

```python
    checks.append(_check("pending", True,
                         f"{status['pending']} lesson(s) waiting for review "
                         f"({status['pending_expired']} expired, not yet swept)"))
```

`src/agent_memory/protocol.py`: replace step 5 of `_CLI_STEPS` with

```
5. When you finish run `amem session end <session-id>`. To leave lessons for a person to review, pipe a JSON list (each: kind, fields, evidence, files) to `amem capture --session <session-id>`.
```

Measure `len(block("cli"))` and `len(block("both"))` (with `PYTHONPATH=src`); raise the limits in `test_protocol_blocks_stay_within_their_length_limits` only as far as needed, to the next 100, keeping `splitlines() <= 12` (cli) and `<= 24` (both). Regenerate goldens (`UPDATE_GOLDEN=1 PYTHONPATH=src python -m pytest tests/test_installer_tools.py -q -k golden`) and read the diff. Update any `tests/test_doctor.py` assertion that lists the exact check names to include `pending`.

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `python -m pytest tests/test_cli_review.py -q` → pass.
Run: `python -m pytest tests -q` → no failures.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory tests
git commit -m "feat: amem capture and amem review (accept needs a terminal), pending in status and doctor"
```

---

### Task 6: Proof — leak eval and two real servers

**Files:**
- Create: `tests/eval/test_pending_leak.py`, `tests/test_capture_two_process.py`

**Interfaces:**
- Consumes: `MemoryService.capture`, `review_accept`; the eval `build`, `service_fns`, `evaluate`, `SCENARIOS`, `HELDOUT`.

- [ ] **Step 1: Write the tests**

Create `tests/eval/test_pending_leak.py`:

```python
"""Pending lessons never reach recall or check, whatever they say, and the scorecard does not
move with 60+ of them in the store. A control proves the test could see a leak: the same lesson
becomes visible the moment a person accepts it."""
import pytest

from agent_memory.service import MemoryService

from .eval_service import service_fns
from .harness import evaluate
from .heldout import HELDOUT
from .run import build
from .scenarios import SCENARIOS

MARK = "pendingleakmarkzx"


def lesson_for(scenario):
    text = f"{MARK} {scenario.task}" if scenario.task else f"{MARK} {scenario.files[0]}"
    return {"kind": "quirk", "fields": {"description": text}, "evidence": "eval lesson",
            "files": scenario.files[:1] or ["README.md"]}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    memory = build(tmp_path_factory.mktemp("shopline-pending"))
    return MemoryService(memory=memory)


def spying(service, seen):
    recall_fn, check_fn = service_fns(service)

    def recall(task, files):
        out = recall_fn(task, files)
        seen.append(out)
        return out

    def check(approach, files):
        out = check_fn(approach, files)
        seen.append(out)
        return out

    return recall, check


def test_pending_lessons_do_not_leak_and_do_not_move_the_scorecard(world):
    before = evaluate(*service_fns(world))
    before_held = evaluate(*service_fns(world), scenarios=HELDOUT)

    lessons = [lesson_for(s) for s in SCENARIOS + HELDOUT if s.task or s.files]
    stored = 0
    for start in range(0, len(lessons), 10):
        stored += len(world.capture(lessons[start:start + 10])["captured"])
    assert stored >= 40 and world.status()["pending"] == stored

    seen = []
    after = evaluate(*spying(world, seen))
    after_held = evaluate(*spying(world, seen), scenarios=HELDOUT)
    assert (after.recall, after.leaks, after.abstention) == (before.recall, before.leaks,
                                                              before.abstention)
    assert after_held.abstention == before_held.abstention
    assert seen and not any(MARK in text for text in seen)

    # control: the same kind of lesson is found once a person accepts it
    scenario = SCENARIOS[0]
    wanted = f"{MARK} {scenario.task}"
    target = next(e["id"] for e in world.review_list()["pending"] if wanted in e["text"])
    world.review_accept(target)
    found = world.recall(scenario.task, scenario.files, max_tokens=2000)["prompt"]
    assert MARK in found
```

Create `tests/test_capture_two_process.py`:

```python
import asyncio
import json
import os
import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters, types          # noqa: E402
from mcp.client.stdio import stdio_client                           # noqa: E402

from agent_memory.service import MemoryService, resolve_config      # noqa: E402
from storage_helpers import SRC                                      # noqa: E402

TEXT = "the zorblax queue drops items over 64KB"


def params(store, repo):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([SRC, env.get("PYTHONPATH", "")])
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_memory.cli", "serve", "--store", str(store), "--repo", str(repo)],
        env=env, cwd=str(repo))


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


def test_a_lesson_captured_on_one_server_stays_invisible_to_the_other_until_accepted(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "cache.py").write_text("x = 1\n")
    store = tmp_path / "shared.memory"
    log = tmp_path / "servers.stderr"

    async def scenario():
        with open(log, "w", encoding="utf-8") as errlog:
            async with stdio_client(params(store, repo), errlog=errlog) as (r1, w1), \
                       stdio_client(params(store, repo), errlog=errlog) as (r2, w2):
                async with ClientSession(r1, w1, client_info=types.Implementation(
                        name="claude-code", version="1")) as a, \
                        ClientSession(r2, w2, client_info=types.Implementation(
                            name="cursor", version="1")) as b:
                    await a.initialize()
                    await b.initialize()
                    captured = await call(a, "memory_capture_lessons", lessons=[{
                        "kind": "quirk", "fields": {"description": TEXT},
                        "evidence": "seen in a test run", "files": ["src/cache.py"]}])
                    ident = captured["captured"][0]
                    before = await call(b, "memory_recall", task="zorblax queue drops items")
                    checked = await call(b, "memory_check",
                                         approach="zorblax queue drops items over 64KB")
                    person = MemoryService(resolve_config(store=str(store), repo=str(repo),
                                                          agent="person"))
                    try:
                        person.review_accept(ident)
                    finally:
                        person.close()
                    after = await call(b, "memory_check",
                                       approach="zorblax queue drops items over 64KB")
                    return ident, before, checked, after

    try:
        ident, before, checked, after = asyncio.run(asyncio.wait_for(scenario(), 120))
    except Exception as exc:
        tail = log.read_text(encoding="utf-8", errors="replace")[-2000:] if log.exists() else ""
        raise AssertionError(f"{type(exc).__name__}: {exc}\nserver stderr (tail):\n{tail}") from exc
    assert before["items"] == [] and checked["items"] == []
    assert [i["id"] for i in after["items"]] == [ident]
    assert after["items"][0]["unreviewed"] is False
```

- [ ] **Step 2: Run**

Run: `python -m pytest tests/eval/test_pending_leak.py tests/test_capture_two_process.py -q`
Expected: PASS (these exercise code from Tasks 1-5; if one fails, the failure is a real defect to fix in the owning task's code, not a reason to weaken the test).
Run: `PYTHONPATH=src python -m tests.eval.run --service` → recall 1.00, 0 leaks, abstention 1.00; held-out abstention 0.38 (check with the held-out run the existing eval documents in `tests/eval/RESULTS.md`).

- [ ] **Step 3: Whole suite**

Run: `python -m pytest tests -q` → no failures.

- [ ] **Step 4: Commit**

```bash
git add tests/eval/test_pending_leak.py tests/test_capture_two_process.py
git commit -m "test: pending lessons never leak into recall; two servers see accept"
```

---

### Task 7: Docs and spec implementation notes

**Files:**
- Modify: `README.md`, `DESCRIBE.md`, `docs/superpowers/specs/2026-11-18-session-lesson-capture-design.md` (add section 10, fix 4.5 wording), `docs/superpowers/specs/2026-11-17-agent-surface-design.md` (status lines only)

- [ ] **Step 1: README and DESCRIBE**

- `README.md`: in the MCP tools table add a row `| memory_capture_lessons | Hand over lessons for a person to review; they are never recalled until accepted |` and extend the `memory_session_end` row with "optionally takes `lessons`". Add a section "Lessons and review" (after the tools section) covering: what a lesson is (`kind`, `fields`, `evidence`, `files`/`symbols`), the limits (10 per call, 100 waiting, 30 days), that nothing is recalled until a person runs `amem review accept ID` in a terminal and types `accept`, `amem review list/show/reject`, `amem capture` for CLI-only agents (JSON list on stdin), and that `status`, `doctor` and `session_start` show the waiting count. Add the error code `queue_full` wherever the README lists codes. In Known limits add: the terminal check is a strong barrier, not proof (a program driving a pseudo-terminal could pass it); evidence is checked for presence and anchors, not truth; accepted lessons are stamped `reviewed` and are the only thing that clears `unreviewed` (legacy `amem add` notes still show as unreviewed).
- `DESCRIBE.md` line ~81: "ten" → "eleven" MCP tools, and one sentence on lessons and review.

- [ ] **Step 2: Spec implementation notes**

In `docs/superpowers/specs/2026-11-18-session-lesson-capture-design.md`:
- section 4.5: replace the sentence "`amem status` shows how many are pending and how many of those have expired but not yet been swept." with "`amem status` shows how many are waiting (`pending`, not expired) and, separately, how many have expired but not yet been swept (`pending_expired`)."
- append `## 10. Implementation notes` listing clarifications 1-10 from the plan (copy their text), the new `amem capture` command, the final protocol length limits actually chosen (read them from `tests/test_installer_tools.py`), the test count and scorecard on the branch, and anything the review rounds changed.

In `docs/superpowers/specs/2026-11-17-agent-surface-design.md`: in the sub-project table row for 3c and in the "open items" line of section 10, note that 3c is built (see its own spec) and that `amem review accept` is the human-review mechanism item 15 asked for.

- [ ] **Step 3: Verify and commit**

Run: `python -m pytest tests -q` → no failures. Run `PYTHONPATH=src python -m tests.eval.run --service` → unchanged.

```bash
git add README.md DESCRIBE.md docs
git commit -m "docs: lessons and review, known limits, spec implementation notes"
```
