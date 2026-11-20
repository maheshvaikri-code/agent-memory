# Session-End Lesson Capture (Pending Queue and Review) — Design (sub-project 3c)

Status: draft for review · Date: 2026-11-18 · Builds on: `2026-11-17-agent-surface-design.md` (3a), `2026-11-16-retrieval-and-record-model-design.md`, `2026-11-17-concurrency-safe-storage-design.md`

## 1. Purpose

Agents using this memory learn things during a task (a quirk, a failed approach, a convention) but only record them if they happen to call `memory_remember` at the right moment. This sub-project gives an agent a deliberate place to hand over what it learned, **without letting an agent write guardrails that other agents then trust unchecked**.

What a person using it should be able to rely on:

- An agent can submit lessons at the end of a session, or at any time, and they are kept.
- A submitted lesson is **never recalled, checked, pinned or carried** until a person accepts it at a terminal. Nothing an agent can call can promote a lesson.
- Every lesson says what it is based on (evidence) and where (a file or symbol), so a reviewer has something to check.
- A flood of bad lessons cannot push out good records or grow the store without bound.
- Recall quality does not change: the scorecard stays at recall 1.00, 0 leaks, abstention 1.00, held-out abstention 0.38 with pending records present.

This also gives the open 3a item "no human-review mechanism clears `unreviewed`" (3a spec §10, item 15) a home.

## 2. Decisions taken with the user

| Question | Decision |
|---|---|
| Where do captured lessons land before they are trusted? | A pending queue until a person accepts. |
| How do agents submit? | Both: a `lessons` argument on `memory_session_end` and a standalone `memory_capture_lessons` tool. |
| How strict is the gate? | Evidence required on every lesson. |
| Who may accept, and how? | A person, in an interactive terminal only. No bypass flag. |

## 3. Out of scope

Automatic extraction of lessons from a transcript or diff (the agent writes its own lessons); a web or MCP review UI; accepting through any non-terminal channel; remote serving (3b); changing retrieval ranking. Review of already-active notes (the older `unreviewed` flag on `explicit` records) stays as it is, except that accepting a lesson produces a reviewed record.

## 4. Design

### 4.1 Record model (engine)

- New status `pending` beside `active`, `superseded`, `retracted`, `archived` (`record.py` `STATUSES`). The retriever already recalls only `status == "active"`, so pending records are excluded from recall, check, pins and carried items without touching ranking. A test pins this rather than relying on it.
- New source `reviewed`, confidence 0.7 (same as `explicit`), added to `SOURCE_CONFIDENCE`. It is **not** in `UNREVIEWED_SOURCES`, so accepting a lesson clears the `unreviewed` flag.
- Pending records live in the same journaled store. Nothing new is ever deleted: rejection retracts, expiry archives.
- A pending record carries `source="extracted"`, confidence 0.5, plus agent and session stamps as any agent write does.

### 4.2 Capture

One code path, `MemoryService.capture(lessons)`, called by both entry points:

- `memory_capture_lessons(lessons)`: any time, no active session needed (stamps a session only if one is active).
- `memory_session_end(summary, completion, lessons=None)`: ends the session as today, then captures.

Per-lesson rules (all must hold, else that lesson is skipped with a reason):

- Same validation as `memory_remember`: one of the ten agent-writable kinds, field length limits, files confined to the repo, the same neutralisation of stored text on output.
- `evidence` is required and non-empty.
- At least one file or symbol anchor.
- `priority=must` is not accepted (agents cannot ask for it here either).

Limits: at most 10 lessons per call; at most 100 pending in the store.

Result: `{captured: [ids], skipped: [{index, reason}]}`. A lesson that would merge with an existing **active** note is skipped and reported as `duplicate`, because a pending record must not silently modify or boost an active one.

`queue_full`: when 100 are pending, `capture` stores nothing and returns that code. An agent cannot evict, retract or replace pending records.

Session end: `memory_session_end` with lessons still ends the session whatever happens in capture. Capture problems, including `queue_full`, appear in a `capture` field of the result rather than raising. A busy store gives `store_busy`, as elsewhere.

A read-only service refuses `capture`, like every other write.

### 4.3 Expiry

Lazy, no daemon: on the next `capture`, `accept` or `reject` (the commands that already write), pending records older than 30 days are set to `archived`. `list`, `show` and `status` are read-only: they do not sweep, but they show a pending record older than 30 days as expired and do not count it toward the 100 limit. The queue therefore cannot stay full forever just because nobody reviewed.

### 4.4 Review (CLI only)

New command group `amem review`:

- `list` and `show ID`: read-only, work on a read-only service. Output is neutralised (terminal escapes stripped), and each body is labelled as agent-written.
- `accept ID...`: requires stdin **and** stdout to be a real terminal, shows each lesson, and asks for confirmation per lesson. There is no `--yes` and no environment override. On accept the record is re-checked as still `pending` and of an agent-writable kind, then set to `active` with source `reviewed` (confidence 0.7).
- `reject ID...`: retracts the record. Does not need a terminal (rejecting is the safe direction).

MCP exposes **no** review tool.

Honest limit: an agent with a pseudo-terminal could satisfy the terminal check. The docs describe this as a strong barrier, not proof. The design relies on the combination of: no MCP route, no flag, and a human-visible confirmation.

### 4.5 Visibility

`amem status`, `amem doctor` and `memory_session_start` report the pending count. Agents never see pending text. `amem status` shows how many are waiting (`pending`, not expired) and, separately, how many have expired but not yet been swept (`pending_expired`).

### 4.6 Protocol text

The MCP protocol block is 1198 of its 1200-character limit (`tests/test_installer_tools.py:161`). Adding the capture clause means raising that limit deliberately, in one visible commit with this spec cited. The CLI protocol block gets a matching sentence. Installer goldens are regenerated in the same commit.

## 5. Security

- Lesson text is untrusted data written by an agent: neutralised on every output path (recall never shows it; review output does, neutralised).
- Flooding is bounded (10 per call, 100 total) and agents cannot evict.
- Accept re-checks status and kind, so a record retracted or changed between `show` and `accept` is not promoted.
- The only elevation path (accept) is not reachable through any tool an agent can call.

## 6. How we will know it works

- A test for every gate rule: missing evidence, missing anchor, bad kind, over-long fields, path outside repo, `must`, more than 10 in a call, 100 pending, duplicate of an active note.
- `capture` partial success: good lessons survive bad ones.
- `memory_session_end` with `lessons` ends the session even when capture fails with `queue_full`.
- Two-process test: a lesson captured on one server is pending and invisible to recall on the other; visible to recall after `accept`.
- `accept` needs a real terminal (test with non-TTY streams: refused); `reject` does not.
- Accepting clears the `unreviewed` flag and makes the lesson recallable.
- Expiry: a 31-day-old pending record is archived on the next capture/review and is not recalled.
- Through-service eval addition: pending lessons never leak into recall; scorecard unchanged with pending records present.
- Full existing suite stays green.

## 7. Decisions I made (change any of them)

- 10 lessons per call, 100 pending, 30-day expiry: chosen to be small and visible; each is a constant that is easy to change.
- Duplicates of active notes are skipped, not merged.
- `reject` does not require a terminal.
- Confidence for accepted lessons equals `explicit` (0.7): a person confirmed it.

## 8. Risks

- A pseudo-terminal can pass the terminal check (see 4.4).
- Evidence is free text; the gate checks presence and anchors, not truth. A reviewer must still judge it.
- Raising the protocol-text limit makes every agent's instruction block slightly longer.
- The pending count could nag users who never review; expiry bounds the cost.

## 9. Plan shape (for the next step)

About six tasks, a reviewer per task, then a whole-branch review:

1. Engine: `pending` status, `reviewed` source, lazy expiry.
2. `MemoryService.capture` and its gate.
3. `memory_capture_lessons` tool, `lessons` on `memory_session_end`, protocol text (limit raised, goldens regenerated).
4. `amem review` (list, show, accept, reject), pending counts in `status` and `doctor`.
5. Eval and leak tests, two-process test.
6. Docs (README, known limits) and implementation notes in this spec.

## 10. Implementation notes

Built as sub-project 3c (commits `fb9c5ce` to `6edef31`). Where the plan had to read the spec more narrowly than it was written, the choice is recorded here.

### 10.1 Clarifications made while planning

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

### 10.2 What was built beyond the spec's list

- `amem capture [--session ID] [--json]` reads a JSON list of lessons on stdin (at most 1,000,000 bytes) so agents without MCP can capture. The CLI protocol block points at it. MCP gained `memory_capture_lessons`, so there are now eleven tools; the per-tool (400) and total (3,000) description limits still hold.
- Protocol length limits, as chosen in `tests/test_installer_tools.py`: the MCP block is at most 1400 characters (the block is 1338; the limit was 1200 and the block 1198), the CLI block at most 1800 (the block is 1702; it was under 1600), and the combined `AGENTS.md` block is still at most 3000 (it is 2685). The golden files were regenerated.

### 10.3 What the review rounds changed

- Engine hardening after the Task 1 review: a pending add refuses `node_id`, and a pending `supersede` is refused, so a pending write can never replace an active note. A pending add forces status `pending` even when a kind's own `status` field says otherwise. `accept` registers the accepted note so that a later identical active write merges into it instead of creating a second record.
- After the Task 5 review: `amem capture` reports deeply nested JSON as `invalid_argument` (no traceback). Ctrl-C or end of input during `amem review accept` stops asking, reports what was already promoted and the rest as skipped, and exits 2.

### 10.4 Verification on the branch

- Full suite: 890 passed, 6 skipped, 8 deselected (793 passed on `main` before this work).
- Scorecard: recall 1.00, 0 leaks, abstention 1.00, held-out abstention 0.38, unchanged with about 66 pending lessons in the eval store (`tests/eval/test_pending_leak.py`).
- Two real stdio servers on one store: a lesson captured through one is seen, and accepted, through the other (`tests/test_capture_two_process.py`).

### 10.5 Known limits, deliberately left

- `amem capture` run on a terminal waits for stdin without saying so.
- Accepting or rejecting an unknown id in a repository with no store creates an empty store.
- The `must` refusal in `review accept` and `review reject` is aimed at agents' permissions and is likely unreachable, because only a person reaches those commands.
- Evidence is checked for presence and for its file or symbol anchors, not for truth; the reviewing person is the check.
- The terminal check on `accept` is a strong barrier, not proof: a program that drives a pseudo-terminal could pass it (section 4.4).
- Accepted lessons are stamped `reviewed` and are the only thing that clears `unreviewed`; legacy `amem add` notes still show as unreviewed.
