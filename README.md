# Agent Memory

Persistent Memory System for Coding Agents using ISONGraph.

## Overview

Agent Memory provides a graph-based memory system that allows AI coding agents to:

- **Remember** project context across sessions
- **Learn** from past interactions and mistakes
- **Maintain** knowledge about codebase architecture
- **Avoid** repeating bugs and violating decisions
- **Apply** project-specific patterns and conventions

## Installation

```bash
pip install agent-memory
```

## Quick Start

### Python API

```python
from agent_memory import AgentMemory

# Initialize memory for a project
memory = AgentMemory("my-project")

# Add conventions
memory.conventions.add_convention(
    category="naming",
    rule="snake_case_functions",
    enforced_by="ruff"
)

# Add architectural decisions
memory.decisions.add_decision(
    title="Use RocksDB over SQLite",
    context="Need fast sequential writes for graph storage",
    consequences_positive=["Fast writes", "Embedded"],
    consequences_negative=["Learning curve"]
)

# Add bug patterns
memory.bugs.add_pattern(
    name="race_condition",
    signature="shared_state_no_lock",
    modules_affected=["cache", "storage"]
)

# Recall what matters for a task
result = memory.recall(
    task="modify the cache module",
    files=["src/cache.py"],
    max_tokens=2000,
)
print(result.to_prompt())

# Check an approach against prior experience
for warning in memory.check("add a global lock around the cache", files=["src/cache.py"]):
    print(warning.text, "-", warning.why)

# Save memory
memory.save("project.memory")
```

### CLI

```bash
# Initialize a memory store
amem init my-project

# Add memories
amem add convention project.memory -c naming -r "snake_case_functions" -e ruff
amem add decision project.memory -t "Use RocksDB" -c "Need fast writes"
amem add bug-pattern project.memory -n "race_condition" -s "shared_state_no_lock"

# Get context for a task
amem context project.memory "modify cache module" -f src/cache.py

# Check a suggestion
amem check project.memory "use SQLAlchemy for the ORM"

# View statistics
amem stats project.memory

# Check memory health
amem health project.memory

# Run consolidation
amem consolidate project.memory

# List memories
amem list conventions project.memory
amem list decisions project.memory
amem list skills project.memory

# Session management
amem start-session project.memory "Add new feature"
amem end-session project.memory s_20240101_1000 -c 0.8

# Export
amem export project.memory -o project.json -f json
```

## Sharing one memory between agents

Several processes on one machine (Claude Code, Codex, a script, an IDE plugin) can use one
memory at the same time. Open it with `AgentMemory.open()` instead of `AgentMemory(...)` or
`load()`; it creates the store when the file does not exist, and everything else in the API
stays the same.

```python
from agent_memory import AgentMemory

with AgentMemory.open("project.memory", repo_root=".", agent="claude-code") as memory:
    session = memory.start_session("add retry logic to the payment webhook")
    memory.remember("convention", {"name": "webhook_retries",
                                   "rule": "cap payment webhook retries at 5 attempts"})
    memory.end_session(session)
```

Another process, at the same time or later:

```python
from agent_memory import AgentMemory

other = AgentMemory.open("project.memory", repo_root=".", agent="codex")
print(other.recall(task="payment webhook retries").to_prompt())   # sees the write above
other.refresh()        # applies other processes' writes now; reads already do it for you
other.checkpoint()     # folds the journal into the snapshot
other.close()          # checkpoints if this process wrote, then refuses further writes
```

What you can rely on:

- A write that returned is durable against the process dying (`durability="fsync"` also
  covers power loss), and every other process sees it on its next read, with no explicit
  refresh.
- Two agents writing at the same moment never lose each other's writes. The same fact written
  by both is merged into one record, and counters (feedback, template use, skill examples)
  add up exactly.
- Killing a process at any instant, mid-write included, never corrupts the store: an
  unfinished write is dropped and every acknowledged one is kept.
- A reader never sees a half-written file.
- On files never opened with `open()`, `AgentMemory(...)`, `load()` and `save()` work as
  before, with two differences: `save()` is now atomic (temp file, then replace), and it
  writes LF line endings (on Windows it used to write CRLF, so a saved file changes its bytes
  once).

A shared store is three files:

| File | Content |
| --- | --- |
| `project.memory` | The snapshot: ordinary ISON that `load()` reads, plus one internal `store_meta` node |
| `project.memory.journal` | One checksummed line per transaction since the last snapshot |
| `project.memory.lock` | Empty; the target of the exclusive OS file lock |

Options to `open()`: `durability="flush"` (default) makes each write survive the process
dying but not a power loss; `durability="fsync"` also survives power loss, at the cost of a
disk sync per write. `lock_timeout` (default 10 s) is how long a call waits for the lock
before raising `StoreLockTimeout`. The journal is folded into the snapshot automatically when
it passes `compact_min_bytes` (default 1 MiB) and is larger than the snapshot, and by
`checkpoint()` and `close()`. `AgentMemory.load()` on a shared store gives a plain,
single-process copy with the journal replayed.

Limits:

- Local filesystems only. Network filesystems (NFS, SMB) are not supported: OS file locks
  are not reliable there. FAT and exFAT are not supported either (coarse timestamps,
  unstable file ids).
- The lock is not fair: a process writing in a tight loop can starve others, which then
  raise `StoreLockTimeout` after `lock_timeout`.
- One `AgentMemory` instance must not be used from several threads at once; give each thread
  its own instance, or serialise the calls.
- Code that iterates `memory.graph.nodes()` or `edges()` while calling store methods should
  iterate a copy (`list(memory.graph.nodes())`): a store call can apply other processes'
  writes to the graph mid-loop.
- After `close()`, store methods and writes raise `ValueError("this store is closed")`;
  `recall()` and `check()` answer from the state the instance last saw.
- `close()` never raises because the lock is busy: if it cannot take the lock within
  `lock_timeout`, it skips its checkpoint (every write is already in the journal) and records
  a note only in the internal `memory._backend.warnings`; no public attribute shows it.
  Call `checkpoint()` yourself when you need to know the snapshot is current (it returns
  `True` when it folded the journal).
- `export_json()` and `save()` to another path include the internal `store_meta` node.
- A store stays shared after every process has closed it: once a path has been opened with
  `open()`, its `.journal` file remains, and a plain `save()` to that path (from `load()`,
  from `AgentMemory(...)`, or from any `amem` write command) raises `StoreInUse`. This is
  deliberate: a plain save over a store that has a journal can write a snapshot whose
  recorded sequence number is stale relative to its content, so the next opener would
  re-apply journal operations. `load()` and the CLI's read commands keep working (`load()`
  replays the journal). The `amem` CLI moves onto `open()` in the next sub-project.

To turn a shared store back into a plain file: make sure no other process is using it, then
open it once, checkpoint, close, and delete the journal and the lock file. After that plain
`load()`/`save()` work again.

```python
import os
from agent_memory import AgentMemory

path = "project.memory"
memory = AgentMemory.open(path)   # replays whatever is left in the journal
assert memory.checkpoint()        # folds all of it into the snapshot
memory.close()
os.remove(path + ".journal")
os.remove(path + ".lock")
```

The explicit `checkpoint()` matters: `close()` checkpoints only when that instance wrote
something, so open-and-close alone can leave journal content outside the snapshot (for
example the writes of a process that crashed without closing). Deleting the journal without
the checkpoint can lose acknowledged writes.

## Memory Types

### 1. Codebase Mental Model
Architecture understanding, module relationships, design patterns.

```python
memory.codebase.add_module("src/cache", "LRU caching layer", complexity="high")
memory.codebase.add_dependency("src/api", "src/cache", coupling="loose")
memory.codebase.add_pattern("singleton", ["src/cache"], why_chosen="single instance needed")
```

### 2. Decisions (ADRs)
Architectural decisions and their rationale.

```python
memory.decisions.add_decision("Use async", "High concurrency expected")
memory.decisions.add_alternative("Use async", "threading", "GIL limitations")
memory.decisions.add_constraint("Use async", "src/api", "all handlers must be async")
```

### 3. Conventions
Code style and project patterns.

```python
memory.conventions.add_convention("naming", "snake_case", "ruff")
memory.conventions.add_idiom("null_check", "if x is None", "if not x")
memory.conventions.add_project_pattern("error_handling", "raise CustomError(code=)", "always")
```

### 4. Bug Patterns
Historical bugs and fixes.

```python
memory.bugs.add_bug("Race condition in cache", severity="critical")
memory.bugs.add_pattern("race_condition", "shared_state_no_lock")
memory.bugs.add_fix_pattern("race_condition", "use asyncio.Lock", "check reentrant need")
```

### 5. Dependencies
Library quirks and workarounds.

```python
memory.dependencies.add_dependency("rocksdb", "0.22.0", "storage")
memory.dependencies.add_quirk("rocksdb", "batch > 4MB causes stall", severity="high")
memory.dependencies.add_workaround("batch > 4MB causes stall", "chunk into 2MB batches")
```

### 6. Test Patterns
Testing conventions and requirements.

```python
memory.tests.add_convention("unit", "test_{module}_{function}_{case}")
memory.tests.add_required_cases("graph_operation", ["empty", "cyclic", "self_loop"])
```

### 7. Performance
Hot paths and constraints.

```python
memory.performance.add_hot_path("node_lookup", "very_high", untouchable=True)
memory.performance.add_constraint("node_lookup", "<5ms p99", "user experience")
```

### 8. Technical Debt
Debt items and refactoring history.

```python
memory.techdebt.add_debt("src/traverse.py", "god function", severity="high")
memory.techdebt.add_refactoring_attempt("src/traverse.py", "split by type", "failed", "circular imports")
```

### 9. Team Knowledge
Who knows what.

```python
memory.team.add_member("Sarah", "contributor")
memory.team.add_expertise("graph_algorithms", "deep", ["src/traverse"])
memory.team.link_expertise("Sarah", "graph_algorithms")
```

### 10. Sessions
Session continuity.

```python
session_id = memory.start_session("Add batch API")
memory.sessions.add_decision(session_id, "use streaming", "memory efficiency", sticky=True)
memory.sessions.add_open_question(session_id, "max batch size?")
memory.end_session(session_id, completion=0.8)
```

### 11. Templates
Code generation patterns.

```python
memory.templates.add_template("api_endpoint", "handler")
memory.templates.add_snippet("async_lock", "async with lock:", "shared state access")
```

### 12. Skills
Agent learning progression.

```python
memory.skills.add_skill("python_async", "language", proficiency=0.7)
memory.skills.record_learning_event("python_async", "success", "handled race condition")
memory.skills.update_proficiency("python_async", 0.8)
```

## Memory Consolidation

The system includes automatic memory consolidation to manage memory over time:

```python
# Run consolidation manually
summary = memory._consolidator.consolidate()

# Check memory health
health = memory._consolidator.get_memory_health()
```

Consolidation archives old session items, decays old records to `archived`
status (it never deletes a record), and merges exact duplicates. Guardrail
kinds (rejected alternatives, bug patterns, failed refactors, quirks, hot
paths, performance constraints) and `priority="must"` records never decay.

## Context Building

`get_context()` still works and returns a `CodingContext` whose lists are a
view over `recall()`; new code should call `recall()`.

## Records

Every memory carries who recorded it, the evidence, a confidence, a scope
and a status.

```python
r = memory.remember(
    "bug_pattern",
    {"name": "cache_stampede", "signature": "concurrent misses recompute the key"},
    anchors=["src/cache.py"],
    evidence="PR 412",
)
memory.supersede(r.id, "bug_pattern",
                 {"name": "cache_stampede", "signature": "concurrent misses recompute the key; fixed by per-key lock"})
memory.feedback(r.id, helpful=True)
memory.verify(r.id)      # the anchored file changed and the memory still holds
memory.retract(r.id, "no longer true")
```

## ISONQL Queries

Query the memory graph directly. ISONQL statements start with `NODES`, `EDGES`,
`TRAVERSE`, `PATH`, `COUNT`, `SUM`, `AVG`, `MIN` or `MAX`:

```python
from ison_graph.query import QueryEngine

engine = QueryEngine(memory.graph)

# Count decisions
result = engine.execute("COUNT decision")
print(result.data)   # [1]

# Find bug patterns for a module
result = engine.execute("NODES bug_pattern WHERE modules_affected CONTAINS 'cache'")
print([n["id"] for n in result.data])   # ['bp_race_condition']

# Follow edges: the modules a decision constrains.
# add_constraint links two existing records, so the module must be added first.
memory.codebase.add_module("src/cache", "LRU caching layer")
memory.decisions.add_constraint("Use RocksDB over SQLite", "src/cache",
                                "cache writes go through the RocksDB batch API")
result = engine.execute("TRAVERSE decision:dec_use_rocksdb_over_sqlite -> CONSTRAINS -> module")
print(result.data)   # [('module', 'mod_src_cache')]
```

## Evaluation

`PYTHONPATH=src python -m tests.eval.run` prints the retrieval scorecard.
The baseline and the current results are recorded in `tests/eval/BASELINE.md`
and `tests/eval/RESULTS.md`. The strict latency gates are opt-in:
`pytest -m perf`. It takes about 2 minutes, because the 5,000-record open test
first builds its store (about 105 s).

## License

MIT
