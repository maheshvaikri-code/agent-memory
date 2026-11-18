# agent-memory — Detailed Capabilities

## Is this useful?

Yes, for the specific failure mode every long-running coding agent hits: it
forgets everything between sessions. Ask it to work on the same codebase
tomorrow and it re-discovers conventions, re-litigates decisions already
made, and repeats bugs it already hit once. `agent-memory` is a persistent
store — conventions, decisions, bug patterns, todos, team knowledge, session
handoffs — that survives across sessions, keyed to a project, queryable at
the start of a task so the agent gets the context instead of relearning it.

This is the most directly "agent workflow" package in the repo, and one of
the largest — 12+ distinct stores, not just a key-value log.

## Maturity

The last full run, `python -m pytest tests`, reported 763 passed, 6 skipped
and 8 deselected. The deselected tests are opt-in: seven strict latency gates
(`pytest -m perf`) and one heavy multi-process stress test (`pytest -m
stress`); the skips are the embeddings test, which needs the optional
embeddings package, and five tests that need symlinks or POSIX permissions
the Windows development machine does not grant. Retrieval quality is gated
by the evaluation suite in `tests/eval`, which scores the same through a
shared store and through the agent-facing service as in memory (must-include
recall 1.00, no leaks, abstention 1.00 in-sample but 0.38 on held-out tasks).
The strict recall gate through the service (p95 under 50 ms at 5,000
records) currently fails on the development laptop once it throttles
under sustained load: p95 about 44-45 ms at full clock, about 86-88 ms
throttled. The cost is the engine's ranking over thousands of candidates
(candidate scoring and de-duplication, about a third of engine time in
de-duplication); the service's own packing adds a few milliseconds.
Shared stores are tested with real processes: concurrent writers, counters
under contention, and processes killed at named points inside a write and a
compaction. On the development laptop a write under contention from four
processes has a p95 of about 3 ms, the check a read makes for other
processes' writes costs about 0.015 ms when there are none, and a
5,000-record store opens in about 0.2 s.

## What it can actually do

**Session lifecycle** — `start_session`/`end_session`; open questions,
blockers and partial work from an unfinished session are carried into the
next `recall()`.

**Recall** — `recall(task, files)` ranks memories by task text, file scope
and graph links, returns nothing when nothing is relevant, and fits the
result into a measured token budget. Each item says why it was returned and
who recorded it.

**Guardrail check** — `check(approach, files)` returns rejected
alternatives, bug patterns and failed refactors relevant to a proposed
approach.

**Provenance and staleness** — every record carries its author, evidence,
confidence and status. A record anchored to a file is flagged when that
file changes, and records are superseded or archived, never deleted.

**Conventions** — record a coding convention once (`add_convention`), and
`check()` flags when a proposed approach conflicts with it.

**Decisions** — architectural decisions with title, context, and rationale,
queryable later ("why did we choose JWT over sessions").

**Bug patterns** — record what went wrong and why, so
`check()` can also catch "we tried this exact approach before
and it broke."

**Todos, tech debt, tests, dependencies, skills, team knowledge, templates,
performance notes** — each its own store with its own query surface, all on
the same graph so they can be cross-referenced (a todo linked to the
decision that created it, a skill linked to the bug pattern that taught it).

**Shared memory** — `AgentMemory.open("project.memory", agent="codex")`
lets several agents on one machine use one memory at the same time. Every
write is durable when the call returns, against the process dying
(`durability="fsync"` also covers power loss), and seen by the other
processes on their next read; two agents writing the same fact get one
record; a process killed mid-write never corrupts the store.

**Agent surface** — `amem serve` is a local stdio MCP server with ten
`memory_*` tools (recall, check, remember, feedback, supersede, retract,
verify, session start/end, status); the same operations are `amem` commands
with `--json` for agents without MCP. `amem install claude-code|cursor|codex|
devin|generic` writes the MCP config and a marked instruction block (dry run
by default, never touching text outside its markers), and `amem doctor`
reports what is wired up. Recalled notes come back in a labelled
untrusted-data block; no tool can delete, choose the store or name a file
outside the repository. Two MCP servers on one store share writes with the
right author (tested with real stdio processes), and the evaluation scores
identically through this boundary as in-process.

**Cache integration** — `cache_integration.py` wraps `ison-cache` so memory
retrieval respects a token budget instead of dumping the whole history.

**Code-flow integration** — `code_flow_integration.py` links memory records
to specific functions/files via `code-flow-graph`, so "what do we know about
`auth.py`" is answerable directly.

## Example: a coding session that remembers what came before

```python
from agent_memory import AgentMemory

memory = AgentMemory("checkout-service", repo_root="/repo", agent="claude-code")
session = memory.start_session("Add retry logic to payment webhook")

memory.remember(
    "bug_pattern",
    {"name": "webhook_storm", "signature": "unbounded retry on external webhook"},
    anchors=["webhooks/payment.py"], evidence="INC-2026-03")

# Recall what matters before writing any code
context = memory.recall(task="add retry logic", files=["webhooks/payment.py"])
print(context.to_prompt())

# Check a specific approach against prior experience
warnings = memory.check("Retry indefinitely with exponential backoff",
                        files=["webhooks/payment.py"])
for warning in warnings:
    print(warning.text)  # names the webhook_storm bug pattern

memory.decisions.add_decision("Cap payment webhook retries at 5 attempts",
                              "Unbounded retry caused INC-2026-03")

memory.end_session(session, completion=1.0)
memory.save("project.memory")

# Next session, or a different agent, starts here:
again = AgentMemory.load("project.memory", repo_root="/repo")
print(again.recall(task="payment webhook retries").to_prompt())
```

## Limitations

- This is per-project memory, not cross-project — there's no built-in
  mechanism for "what did we learn on other projects that also applies
  here."
- Memory quality is only as good as what gets recorded; nothing here
  automatically extracts conventions or decisions from a diff — the agent
  (or workflow) has to record them explicitly (`memory_remember`,
  `amem remember` or the Python API). The installed instruction block tells
  agents to, but an agent may not follow it.
- The agent surface is local: a stdio MCP server and a CLI on the same
  machine. Hosted agents that need an MCP server by URL (Base44) are not
  served yet, and tool support for Devin, Codex and Cursor beyond the files
  the installer writes comes from secondary sources and is unverified.
- On a set of held-out unrelated tasks, recall stays empty only 6 of 16
  times (0.38): a single shared word can let an unrelated note through. It
  is labelled with why it matched, but it costs tokens.
- Matching is lexical unless the optional embedding channel is attached, so
  a paraphrase that shares no word with a memory will not find it.
- Staleness is tracked per file, not per function; a busy file marks every
  memory anchored to it as changed.
- Only processes on one machine can share a memory, and only through
  `AgentMemory.open()`. Plain `AgentMemory(...)`, `load()` and `save()` stay
  single-process: two processes that each load and save the same plain file
  still overwrite each other. Network filesystems (NFS, SMB) and FAT/exFAT
  are not supported.
- A path once opened with `open()` stays a shared store after every process
  has closed it: its `.journal` file remains, and a plain `save()` to it
  raises `StoreInUse` (the `amem` commands use `open()`, and on a plain
  `.memory` file they convert it, one way), because a plain save there could write a snapshot
  whose recorded sequence number is stale and make the next opener re-apply
  journal operations. To return it to a plain file, with no other process
  using it: `AgentMemory.open(path)`, `checkpoint()` (go on only if it
  returns `True`), `close()`, then delete `<path>.journal`. Deleting the
  journal without that checkpoint can lose acknowledged writes (`close()`
  alone checkpoints only when that instance wrote). Leave `<path>.lock`
  alone: plain mode ignores it.
- `open()` refuses `.isonl` paths (`ValueError`, no file touched); plain
  `load()`/`save()` of `.isonl` work as before. A store is identified by its
  real path, so symlinked names share it; hard links do not.
- `StoreCorrupt` means the store's files are damaged; the message names the
  file and the byte or line, and nothing is rewritten. Restore the snapshot
  from a backup or inspect the journal; do not delete the journal, which
  holds every write since the last checkpoint.
