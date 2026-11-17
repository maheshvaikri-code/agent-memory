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

79 tests, all passing, untouched by bug fixes this session — it was already
in solid shape. It's also the second-largest source file in the repo by
line count (`governed_memory.py` at 912 lines, `integration.py` at 777).

## What it can actually do

**Session lifecycle** — `session_start`/`session_end` with completion score,
outcome, and lessons learned; `get_handoff` produces a structured summary
for the next session (or the next agent) to pick up from.

**Context retrieval** — `get_context(task, files)` returns the conventions,
past decisions, and known bug patterns relevant to the specific task and
files about to be touched, not the whole project history.

**Conventions** — record a coding convention once (`add_convention`), and
`check_before_coding` flags when a proposed approach conflicts with it.

**Decisions** — architectural decisions with title, context, and rationale,
queryable later ("why did we choose JWT over sessions").

**Bug patterns** — record what went wrong and why, so
`check_before_coding` can also catch "we tried this exact approach before
and it broke."

**Todos, tech debt, tests, dependencies, skills, team knowledge, templates,
performance notes** — each its own store with its own query surface, all on
the same graph so they can be cross-referenced (a todo linked to the
decision that created it, a skill linked to the bug pattern that taught it).

**Cache integration** — `cache_integration.py` wraps `ison-cache` so memory
retrieval respects a token budget instead of dumping the whole history.

**Code-flow integration** — `code_flow_integration.py` links memory records
to specific functions/files via `code-flow-graph`, so "what do we know about
`auth.py`" is answerable directly.

## Example: a coding session that remembers what came before

```python
from agent_memory import AgentMemory

memory = AgentMemory(project_id="checkout-service", project_path="/repo")

memory.session_start(task_description="Add retry logic to payment webhook")

# Get everything relevant before writing any code
context = memory.get_context(
    task="add retry logic",
    files=["webhooks/payment.py"],
)
# context includes: conventions tagged 'webhooks' or 'retries',
# past decisions touching payment.py, and any bug patterns matching

# Check a specific approach against known issues before committing to it
warning = memory.check_before_coding(
    approach="Retry indefinitely with exponential backoff",
)
if warning:
    print(warning)  # e.g. "Bug pattern BP-14: unbounded retry caused a
                     #  webhook storm in incident INC-2026-03; cap retries"

memory.add_decision(
    title="Cap payment webhook retries at 5 attempts",
    context="Unbounded retry caused INC-2026-03",
)

memory.session_end(
    completion=1.0,
    outcome="success",
    lessons=["Always cap retries on external webhooks, no exceptions"],
)

# Next session, or a different agent, starts here:
handoff = memory.get_handoff()
```

## Limitations

- This is per-project memory, not cross-project — there's no built-in
  mechanism for "what did we learn on other projects that also applies
  here."
- Memory quality is only as good as what gets recorded; nothing here
  automatically extracts conventions or decisions from a diff — the agent
  (or workflow) has to call `add_convention`/`add_decision` explicitly.
