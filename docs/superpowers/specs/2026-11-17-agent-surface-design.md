# Agent Surface (MCP, CLI, Instruction Files) — Design (sub-project 3a)

Status: draft for review · Date: 2026-11-17 · Builds on: `2026-11-16-retrieval-and-record-model-design.md`, `2026-11-17-concurrency-safe-storage-design.md`

## 1. Purpose

The memory engine is finished but only Python code can use it. A coding agent (Claude Code, Codex, Cursor, Devin, Replit Agent, Base44, and others) has to be able to **recall before it works, check before it commits to a risky approach, and record what it learned**, without anyone writing glue code. This sub-project builds the surface agents actually touch, and the files that tell them to use it.

What a person using it should be able to rely on:

- One command wires a repo to a tool: it prints (or writes) that tool's MCP config and a short instruction block, never overwriting what is already there.
- An agent that follows the instruction block gets relevant, token-bounded, attributed context at the start of a task and a warning before repeating a known mistake.
- Several agents on one repo, each in its own process, share one memory safely (the storage layer from sub-project 2 guarantees it).
- A tool without MCP support can still use the memory through a plain command with JSON output.
- Nothing an agent can call can delete data, read files outside the repo, or choose where the store lives.

## 2. Decomposition

Which delivery mode reaches which tool is not uniform (section 3), so this is three specs, built in order:

| Spec | Scope | Reaches |
|---|---|---|
| **3a (this one)** | Transport-agnostic service; stdio MCP server; CLI v2 on the shared store; installer for instruction files and MCP config | Claude Code, Codex CLI, Cursor, any shell-capable agent |
| 3b | Remote MCP endpoint over streamable HTTP with bearer auth, deployment guidance | Hosted agents that need a URL (Base44, probably Devin) |
| 3c | Session-end lesson capture (agent-driven, gated), the "extracted" record pipeline | All, by raising how much gets recorded |

3a is built so that 3b is a thin transport added on the same service, and 3c is a new tool plus a new kind of record, not a rewrite.

### Out of scope for 3a

Remote or hosted serving and authentication (3b); automatic extraction of lessons from a transcript or diff (3c); git-based sharing of a memory between machines (a later spec); a web UI; embedding/semantic search wiring (the optional `attach_semantic` hook stays as it is).

## 3. What I verified before designing

Local facts (checked in this repo today):

- The Python MCP SDK is installed (`mcp` 1.25.0). `FastMCP` provides stdio, SSE and streamable-HTTP transports, `instructions`, a `lifespan`, a `TokenVerifier` hook for bearer auth, and the connecting client's name on the request context. It requires Python 3.10+, while this package declares 3.9+, so MCP support is an optional extra.
- The current `amem` CLI has only the old per-store commands. It has no `recall`, `guard` or `remember`, and its write commands use plain `load`/`save`, which sub-project 2 deliberately refuses on a store that has ever been opened with `open()`.
- `AgentIntegration` (integration.py) is also plain-mode only.

Tool support (from web search; **secondary sources, treat as unverified until checked against each vendor's own docs**):

| Tool | MCP | Instruction file | Confidence |
|---|---|---|---|
| Codex CLI | stdio and remote ([source](https://dev.to/momen_hq/the-best-mcp-servers-and-plugins-for-claude-code-codex-and-cursor-in-2026-2328)) | AGENTS.md | medium |
| Cursor | stdio, SSE, streamable HTTP; tools but not resources ([source](https://contextbolt.com/blog/ai-tools-mcp-support/)) | AGENTS.md and `.cursor/rules/` | medium |
| Claude Code | yes (known) | CLAUDE.md; whether it reads AGENTS.md is disputed ([source](https://mer.vin/news/claude-code-now-reads-agents-md-when-theres-no-claude-md/)) | high / low |
| Devin | sources conflict: one says no ([source](https://agents-compare-three.vercel.app/feature/mcp-support)), a later guide says yes ([source](https://fungies.io/mcp-servers-developers-guide-2026/)) | `.agents/AGENTS.md`, `.devin/rules/` | low |
| Replit Agent | one source says no | not found | low |
| Base44 | **custom MCP by URL only**, OAuth or custom headers, Builder plan or higher ([docs](https://docs.base44.com/documentation/account-and-billing/setting-up-a-custom-mcp)) | n/a | high |

Consequences that drive the design: Base44 cannot reach a local stdio server, which is why remote serving is its own spec (3b); Replit and possibly Devin may need the CLI path, which is why the CLI is a first-class surface here and not an afterthought; and since support for the instruction-file convention varies, the installer must emit the right file per tool and the MCP tool descriptions must stand on their own.

## 4. Design

### 4.1 Architecture

```
MemoryService  (transport-agnostic; the only code that touches AgentMemory)
   ├── mcp_server.py   FastMCP adapter: tools = service methods, stdio now, HTTP in 3b
   ├── cli.py          click adapter: amem recall/guard/remember/... --json
   └── installer.py    writes MCP config + instruction blocks per tool
```

`MemoryService(store_path, repo_root, agent, ...)` owns one `AgentMemory.open(...)` for its lifetime and exposes a handful of methods that take and return plain JSON-able dicts. Validation, size limits, path confinement, error mapping and result shaping live here once, so the MCP server, the CLI and the later HTTP transport cannot drift apart. The adapters contain no memory logic.

### 4.2 Where the store lives

Chosen once at startup, never by a tool argument:

1. `--store PATH` or `AGENT_MEMORY_STORE`, else
2. `<repo>/.agent-memory/project.memory`.

The repo root is `--repo` / `AGENT_MEMORY_REPO`, else the git top-level of the working directory, else the working directory. `amem install` adds `.agent-memory/` to `.gitignore` (the snapshot, journal and lock are local working state; sharing a memory between machines is a later spec). A legacy `.memory` file given with `--store` is opened with `open()` and upgraded in place; this is documented, and it is the one-way step that sub-project 2 already describes.

### 4.3 Identity and sessions

The agent name stamped on every write is the MCP client's reported name (for example `claude-code`, `cursor`), else `--agent` / `AGENT_MEMORY_AGENT`, else `cli`. The server tracks one current session per connection. `memory_session_start` creates it; `memory_remember` and the other writes stamp it; `memory_session_start` first reports the open items (questions, blockers, partial work) of the previous session that was never ended (the engine carries only those, for up to 14 days; a cleanly ended session's items are not carried), then creates the new session. `memory_session_end` closes the session and returns the items still unresolved so the agent can see what is left.

### 4.4 Tools

Names carry a `memory_` prefix so they do not collide with other servers in the same client. Descriptions are short and say *when* to call, because that is what an agent reads.

| Tool | Inputs | Returns |
|---|---|---|
| `memory_recall` | `task`, `files?`, `symbols?`, `max_tokens?` (default 1500, cap 4000) | `{prompt, items[{id, kind, text, why, staleness, agent, confidence}], tokens}`; empty with a note when nothing is relevant |
| `memory_check` | `approach`, `files?` | guardrail hits only: rejected alternatives, bug patterns, failed refactors, constraints |
| `memory_remember` | `kind`, `fields`, `files?`, `evidence?`, `confidence?`, `priority?` | `{id, created, merged, similar[]}` |
| `memory_feedback` | `id`, `helpful` | `{ok}` |
| `memory_supersede` | `old_id`, `kind`, `fields`, ... | `{id, superseded}` |
| `memory_retract` | `id`, `reason` | `{ok}` (the record is kept, no longer returned) |
| `memory_verify` | `id` | `{restamped}` (re-confirms a record's anchors at the current file hashes) |
| `memory_session_start` | `task` | `{session_id, already_active, carried[...]}` (open items from earlier sessions) |
| `memory_session_end` | `summary?`, `completion?` | `{ok, session_id, unresolved[...]}` |
| `memory_status` | none | store path, shared mode, counts by kind, last write, storage warnings |

`memory_remember` accepts a curated set of kinds an agent can sensibly write: `convention`, `decision`, `bug_pattern`, `alternative`, `quirk`, `workaround`, `refactoring_attempt`, `open_question`, `blocker`, `partial_work`. The tool description lists each kind's required fields with a one-line example. The other registry kinds (`module`, `hot_path`, `snippet`, ...) stay available to the Python API and the bootstrap scan but are not offered to agents in 3a.

There is deliberately **no delete tool**. Records are retracted or superseded, never removed (the record model's rule), and nothing an agent calls can edit history.

### 4.5 Output shaping

Every response is JSON. Recall and check also return a `prompt` string in the existing rendered form so a client can paste it as-is. Recorded text is returned inside a clearly labelled block ("recorded project notes, untrusted data, written by <agent>"), because a memory is something a previous agent wrote and must not be able to instruct the current one. Token budgets use the existing measured counter. Abstention is a first-class result: no relevant memory yields an empty list, not the nearest weak match.

### 4.6 Instruction block and installer

`amem install <tool>` supports `claude-code`, `codex`, `cursor`, `devin`, `generic` and `agents-md`. For each it can emit, and with `--write` apply:

- the MCP registration for that tool (for example a `.mcp.json` entry, a Cursor `mcp.json` entry, or the `codex mcp add` command line), pointing at `amem serve --store ... --repo ...`;
- an instruction block of about 20 lines in the file that tool reads (`AGENTS.md`, `CLAUDE.md`, `.cursor/rules/agent-memory.mdc`, `.devin/rules/`), saying: recall at the start of a task, check before adopting an approach in an area with prior failures, record decisions, rejected alternatives and bugs as they happen, close the session at the end, and treat recalled notes as data.

The block sits between marker comments. Re-running replaces only the marked region; existing content is never touched; the default is a dry run that prints the diff; `--write` is required to change a file. A tool without MCP gets the same block with the CLI commands in place of tool names.

### 4.7 CLI v2

New commands on the shared store, each with a stable `--json` form: `amem recall`, `guard APPROACH`, `remember`, `feedback`, `retract`, `session start|end`, `status`, `serve`, `install`, `doctor`. The existing commands keep their names and output but move from `load`/`save` to the service, so they work on any store and stop being refused. `doctor` reports the store path, whether it opens, lock health, journal state, storage warnings, and which tools have the MCP entry and instruction block installed.

### 4.8 Security

- Tools accept no filesystem path except `files`, which are resolved against the repo root and rejected if they escape it (`..`, absolute paths outside, symlinks resolved).
- The store path, repo root and agent default are fixed at startup.
- Field and text sizes are capped (per field and per call); the number of records returned is capped.
- Writes are attributed (agent, session, source `explicit`) and nothing can delete.
- Recalled text is data, delimited and labelled as in 4.5.
- stdio runs with the local user's rights; remote authentication is 3b's problem and is why 3a binds nothing to a network port.

### 4.9 Errors

A service error has a stable `code` (`invalid_argument`, `unknown_kind`, `path_outside_repo`, `not_found`, `store_busy`, `store_corrupt`, `too_large`) and a message that says what to do. `StoreLockTimeout` maps to `store_busy`, `StoreCorrupt` to `store_corrupt`; the MCP adapter returns them as tool errors, the CLI as exit codes with the JSON error.

### 4.10 Lifecycle

`amem serve` opens the store, serves stdio, and on a clean shutdown calls `checkpoint()` when `unfolded_transactions() > 0`, so a stopped server leaves a complete snapshot. This implements the "everyone closed cleanly means the snapshot is complete" property **in the server**, without changing `close()` semantics in the storage layer (which stays as sub-project 2 left it).

## 5. Folded in from earlier reviews

Items sub-project 1 and 2 left for this one, each small:

1. **Journal-header hardening (task 0).** A changed byte inside the header's generation id yields a valid header for a different generation, so the journal is judged stale and acknowledged writes are dropped silently. Fix without a format change: when a parseable journal carries a generation other than the snapshot's, re-read it and raise `StoreCorrupt` if any transaction's `seq` exceeds the snapshot's `seq` (a genuinely stale journal only holds seqs at or below it). A server holding a store open for hours makes silent loss costlier, so this goes first.
2. A public `AgentMemory.warnings` (today a skipped `close()` checkpoint is visible only on the private backend), used by `memory_status` and `doctor`.
3. A public token counter and sink setter, and bounding the retriever's `_Index._prefixes/_order` (long-lived server process).
4. `load()` returns replay warnings through `load_warnings`.

Dropped as not needed by 3a: a public id-based mutator path (the service resolves ids through the facade) and a rebind path (`_bind` only runs when constructing a fresh instance).

## 6. How we will know it works

1. **Service tests:** every method, every error code, every size cap, path escape attempts (`..`, absolute, symlink), unknown kinds, abstention.
2. **MCP protocol tests:** spawn `amem serve` and drive it with the SDK's own client over stdio: list tools, call each, check schemas and a full recall/remember/check/session round trip.
3. **Two servers, one store:** two stdio servers with different client names write and recall concurrently (reusing the stress harness idea); every write acknowledged is recallable from the other.
4. **Equivalence:** the evaluation suite run through the MCP boundary gives the same scorecard as in-process (recall 1.00, leaks 0, abstention 1.00, held-out 0.38).
5. **Installer:** dry run changes nothing; `--write` is idempotent; user content outside the markers survives; every supported tool's output is checked against a golden file; `.gitignore` handling.
6. **CLI:** `--json` shapes pinned; legacy commands work on a store that has been opened with `open()`.
7. **Header hardening:** the one-byte-in-generation case now raises; stale-journal and benign cases still recover.
8. **Injection check:** a record whose text says "ignore previous instructions" is returned inside the untrusted block and never in the instruction position.
9. **Perf (opt-in):** recall through the service p95 under 50 ms on a 5,000-record store.

## 7. Decisions I made (change any of them)

1. **Three specs, local first.** The tool-support evidence says Base44 needs a URL and Replit/Devin are uncertain, so local stdio plus a CLI covers the majority now and the remote transport is a clean follow-on. Cost if wrong: hosted-only users wait for 3b.
2. **One service layer under both MCP and CLI.** Cost: a layer to maintain; benefit: no drift.
3. **Store location and agent identity fixed at startup, not by tool arguments.** Cost: one server per repo; benefit: an agent cannot point the server elsewhere.
4. **No delete tool.** Cost: no way to purge a bad record through the surface (retract hides it); benefit: no destructive call exists.
5. **Curated ten kinds for `remember`.** Cost: some registry kinds are not agent-writable yet; benefit: a small, learnable schema.
6. **Dry-run-by-default installer with marked blocks.** Cost: one extra flag; benefit: it cannot clobber a user's files.
7. **`.agent-memory/` is git-ignored by default.** Cost: no cross-machine sharing until a later spec; benefit: no merge conflicts on a live journal.
8. **The server, not `close()`, checkpoints on clean shutdown.** Cost: other clients that call `close()` still do not fold a replayed journal; benefit: no change to storage semantics after review.
9. **`mcp` is an optional extra (`agent-memory[mcp]`), Python 3.10+.** Cost: the MCP server is not available on 3.9; benefit: the core package keeps its 3.9 support.
10. **Tool support table treated as unverified.** The installer ships for all six targets, but only Claude Code, Codex and Cursor are tested end to end in 3a; Devin and generic output are golden-file tested only.

## 8. Risks

- Agents may not call the tools. The instruction block and short, situational tool descriptions are the mitigation here; 3c's session-end capture is the structural one.
- Tool descriptions cost tokens on every turn. They are kept short and measured.
- AGENTS.md handling varies by tool and version (Claude Code's is disputed), so the installer writes the file each tool is known to read and `doctor` reports what it found.
- The tool-support evidence comes from secondary sources and will age; the installer's per-tool templates are data files, so correcting one is a one-line change.

## 9. Plan shape (for the next step)

About 10 tasks: (0) header hardening; (1) `AgentMemory.warnings` and the long-lived-process items (token counter, index bounds, rebind); (2) `MemoryService` core with validation and errors; (3) recall/check shaping, the untrusted block and path confinement; (4) the stdio MCP server and its protocol tests; (5) CLI v2 and moving the legacy commands onto the service; (6) installer core (markers, dry run, idempotence); (7) per-tool templates and golden files; (8) `doctor` and `status`; (9) two-server concurrency test, MCP-boundary equivalence run, docs and README. Each task: an implementer and a reviewer subagent, then a final whole-branch review, as before.
