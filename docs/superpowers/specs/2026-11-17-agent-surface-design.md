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

The agent name stamped on every write is the MCP client's reported name (for example `claude-code`, `cursor`), else `--agent` / `AGENT_MEMORY_AGENT`, else `cli`. The server tracks one current session per connection. `memory_remember` and the other writes stamp the current session; `memory_session_start` first reports the open items (questions, blockers, partial work) of the previous session that was never ended (the engine carries only those, for up to 14 days; a cleanly ended session's items are not carried), then creates the new session. `memory_session_end` closes the session and returns the items still unresolved so the agent can see what is left.

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
| `memory_session_start` | `task` | `{session_id, already_active, carried[...]}` (open items of the previous never-ended session) |
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

About 10 tasks: (0) header hardening; (1) `AgentMemory.warnings` and the long-lived-process items (token counter, index bounds); (2) `MemoryService` core with validation and errors; (3) recall/check shaping, the untrusted block and path confinement; (4) the stdio MCP server and its protocol tests; (5) CLI v2 and moving the legacy commands onto the service; (6) installer core (markers, dry run, idempotence); (7) per-tool templates and golden files; (8) `doctor` and `status`; (9) two-server concurrency test, MCP-boundary equivalence run, docs and README. Each task: an implementer and a reviewer subagent, then a final whole-branch review, as before.

## 10. Implementation notes (what was built, and where it differs from sections 1-9)

Built as specified unless listed here: `MemoryService` (`agent_memory.service`), the stdio MCP server (`amem serve`, `agent_memory.mcp_server`, the only module that imports `mcp`), CLI v2 (`agent_memory.cli_agent`), the installer (`agent_memory.installer`) and `amem doctor` (`agent_memory.doctor`).

Deviations:

1. **Session carrying (4.3).** Corrected before building: the engine carries open items only from a session that was never ended. `memory_session_start` reports them before creating the new session; `memory_session_end` returns what is still unresolved.
2. **`amem guard`, not `amem check` (4.7).** `amem check MEMORY_FILE SUGGESTION` already existed and still works, so the new command is `amem guard APPROACH`.
3. **Section 5:** the id-based mutator path and the rebind path were dropped as not needed; the server checkpoints at shutdown only when `unfolded_transactions() > 0` (4.10).
4. **Five installer tools, not six (4.6).** `claude-code`, `cursor`, `codex`, `devin`, `generic`; there is no separate `agents-md` target (`generic` is it). Devin writes the root `AGENTS.md`, not `.devin/rules/` or `.agents/AGENTS.md`, until its paths are verified. Every tool that reads `AGENTS.md` gets one combined block (MCP tool names and the CLI commands), so installs commute instead of flipping the block's mode. Codex and Devin get a printed registration command; only Claude Code (`.mcp.json`) and Cursor (`.cursor/mcp.json`) get a written MCP entry. The registration is `amem serve` with no `--store` or `--repo` by default (4.6 said `amem serve --store ... --repo ...`): the server finds the repository from the working directory the client starts it in, which keeps the config portable; `amem install --store PATH` pins a store.
5. **Recall `context` (added).** `memory_recall` takes an optional `context` (`amem recall --context TEXT|-`), at most 2000 characters. Only identifier-like terms are used (mixed case, snake_case parts, letter-digit words, ALL-CAPS codes; at most 40 terms; log and test-runner words dropped), appended to the query as one extra symbol. Raw text was tried first and ended abstention on every filtered scenario. Omitted, recall is byte-identical to before.
6. **Neutralizing recorded text (4.5).** Every string returned (the prompt and each item's fields) is NFKC-folded, format characters (Cf) and control characters other than newline and tab are dropped, and every `<` is escaped as `&lt;`, so no spelling of a tag can open or close the untrusted block. The block's preamble names its authors as "AI coding agents and people in earlier sessions" (each note's author is on its why line). `items[].id` is the exception: only `&` and `<` are escaped (reversibly) and the service reverses that on input, so a legacy id round-trips exactly.
7. **Recall budget.** `WRAPPER_TOKENS = 110` (the labelled block's own cost) is taken from `max_tokens` before the engine packs; the service then re-packs the engine's items against the wrapped block in rank order, skipping an item that does not fit and going on, and says in `note` how many relevant notes did not fit. An empty recall ranks a second time without a budget to tell "nothing relevant" from "did not fit", so it costs two rankings.
8. **Error codes (4.9).** Two codes beyond the spec's list: `no_active_session` and `internal`. Any exception that is not a store or argument error leaves as `internal` with a generic message (the server logs the detail to stderr); no traceback or path reaches the agent. FastMCP prefixes a tool error's text with `Error executing tool <name>:`, and type errors raised by the MCP layer itself carry no stable code. On the CLI a usage error under `--json` is still click's plain text (exit 2).
9. **Stricter than specified.** Agents cannot retract or supersede a record of a kind outside the ten agent-writable kinds, nor a `must` note unless the service runs with `allow_must`; control characters and the repository root itself are refused as `files`.
10. **Doctor.** It never creates a store, reads a plain (legacy) `.memory` file with `load()` instead of opening it (an open converts it in place, one way), does not checkpoint on its way out, and treats a missing `mcp` package as a note (`ok: true`), since only `amem serve` needs it. A `.journal` holding writes without its snapshot is reported as `store_corrupt` (read, not opened). Each check runs on its own, so one that fails unexpectedly is reported as failed without dropping the others. For Codex and Devin it can see only the `AGENTS.md` block, and since codex, devin and generic share that block it reports all three as installed once any of them is.
11. **Legacy CLI writes (4.7).** 4.7 said the existing commands move to the service. They do not: `amem add ...`, `start-session`, `end-session` and `consolidate` call `AgentMemory.open()` directly (`cli._writable`), so they stop being refused on a shared store; on a plain `.memory` file they convert it in place to a shared store, one way. An `.isonl` file keeps plain `load()`/`save()`. The read commands keep `load()`.

Verification as built (section 6):

- **Equivalence (6.4)** runs the evaluation through `MemoryService` in-process (`tests/eval/test_through_service.py`, `python -m tests.eval.run --service`), judging the wrapped prompt, not through stdio MCP: the transport adds no logic, and the stdio round trip is covered by `tests/test_mcp_server.py` and the two-server test. Scorecard identical to the engine's: recall 1.00 (30/30), leaks 0, abstention 1.00 (12/12), held-out 0.38 (6/16).
- **Two servers (6.3):** `tests/test_mcp_concurrency.py` starts two real `amem serve` processes with different client names on one store; each recalls the other's write with the right author, and the same fact from both is one record. 6.3 asked to reuse the stress-harness idea (concurrent writers); this test interleaves the two servers' calls one after another, so it proves cross-process visibility and attribution through MCP, not write contention, which the storage layer's own process tests (`tests/test_concurrency.py`, `-m stress`) cover.
- **Perf (6.9):** `tests/test_service_perf.py::test_recall_p95_under_50ms_on_5000_records` is opt-in (`-m perf`); a loose always-on guard (mean under 250 ms on 300 records) runs by default. **The 50 ms gate currently fails on the development laptop**, measured, not promised. At full clock recall through the service on 5,000 conventions takes about 36-42 ms median, p95 about 44-45 ms. After a few seconds of sustained load the laptop throttles (a fixed pure-Python workload slows about 2.7x), and the p95 over 100 recalls rises to about 86-88 ms (it was 118-125 ms before the re-pack fix below); the test's own run, right after its 6-minute build, failed with p95 100.0 ms (128.8 ms before the fix). The cost is the engine's ranking over thousands of candidates: candidate scoring and de-duplication, with de-duplication about a third of engine time. The service used to add a fixed 6-7 ms by re-rendering and re-neutralizing the whole prompt for every item it tried; it now renders and neutralizes each item once (`shaping.wrap_items`), with output byte-identical to the old algorithm (`tests/test_service_repack.py`). That cut recall at 300 records from about 21 ms to about 5 ms median. The prefix cache and the empty recall's second ranking are not involved: an unbounded cache measured the same, and no timed recall was empty.
- Tool support (section 3) is unchanged: nothing was verified against the vendors' own documentation, so Devin, Codex and Cursor details stay secondary-source facts (`verified=False` for Devin in `installer/tools.py`).

Open items: remote MCP for hosted tools (3b); session-end lesson capture (3c); the held-out abstention weakness (0.38); a sink-safe probe for the empty recall's second ranking once a usage sink is wired; the retriever's prefix cache clears all entries past 4,096 texts; the recall p95 bound at 5,000 records is not met on a throttling laptop (the engine's ranking is the remaining cost); the engine can rank a different item set for an identical repeated recall (rarely; found by the re-pack equivalence test, which therefore replays engine answers); a SIGTERM handler and per-connection sessions for a long-lived or shared server (3b).
