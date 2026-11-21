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

## Using the memory from an agent

A coding agent reaches the memory through a local MCP server (`amem serve`, over stdio) or
through `amem` commands with `--json` output. `amem install` wires a repository to a tool.

### Install

```bash
pip install 'agent-memory[mcp]'     # the MCP server needs Python 3.10+
pip install agent-memory            # CLI only (Python 3.9+): every command except `amem serve`
```

### Wire a repository to a tool

```bash
amem install claude-code            # dry run: prints the diff, writes nothing
amem install claude-code --write    # applies it
```

Tools: `claude-code`, `cursor`, `codex`, `devin`, `generic`. What each one gets:

| Tool | Instruction block in | MCP registration |
| --- | --- | --- |
| `claude-code` | `CLAUDE.md` | `agent-memory` entry in `.mcp.json` |
| `cursor` | `.cursor/rules/agent-memory.mdc` | `agent-memory` entry in `.cursor/mcp.json` |
| `codex` | `AGENTS.md` | prints `codex mcp add agent-memory -- amem serve` for you to run |
| `devin` | `AGENTS.md` | prints `amem serve` to register in Devin's own settings |
| `generic` | `AGENTS.md` | none: the block tells the agent to use the `amem` commands |

Every install also adds `.agent-memory/` to `.gitignore` when the repository has a `.git`.
The instruction block sits between `<!-- agent-memory:begin ... -->` and
`<!-- agent-memory:end -->` markers. Re-running replaces only that region, so running it twice
changes nothing; text outside the markers, other MCP servers and other keys in the JSON config
are never touched. An existing config that is not valid JSON, or a file with broken markers,
is refused rather than rewritten. The tools that read `AGENTS.md` (`codex`, `devin`,
`generic`) share one combined block that names the MCP tools and the `amem` commands, so
installing several of them in any order gives the same file. `--store PATH` pins the store in
the server command (by default the server finds it from the repository, which keeps the
config portable); `--command` changes the command that starts the server (default `amem`).

### What the agent then does

The block (and the MCP server's own instructions) tell the agent to:

1. start a session and recall what is recorded for the task and the files it expects to touch;
2. check an approach before adopting it in an area with past problems;
3. record each decision, convention, bug root cause, rejected alternative, quirk, workaround,
   failed refactor and piece of unfinished work as it happens;
4. give feedback on a recalled note that helped or misled, and retract one that is wrong;
5. end the session.

### MCP tools

| Tool | What it does |
| --- | --- |
| `memory_recall` | Notes relevant to `task` (and `files`, `symbols`, optional `context`), within `max_tokens` (default 1500, clamped to 200..4000). Empty, with a `note`, when nothing is relevant |
| `memory_check` | Recorded failures (rejected alternatives, bug patterns, failed refactors, constraints) that match a proposed `approach`; `warn` is true when one does |
| `memory_remember` | Record one fact of an agent-writable kind; the same fact written twice is merged into one record |
| `memory_feedback` | Say a recalled note helped or misled |
| `memory_supersede` | Replace a note with a corrected one; the old one is kept, no longer returned |
| `memory_retract` | Mark a note as wrong; it is kept, no longer returned |
| `memory_verify` | Re-stamp a note's file anchors after re-reading the code |
| `memory_session_start` | Start a session; returns open items of an earlier session that was never ended |
| `memory_session_end` | End the session; returns what is still unresolved. Optionally takes `lessons` (see "Lessons and review") |
| `memory_capture_lessons` | Hand over lessons for a person to review; they are never recalled until accepted |
| `memory_status` | Store, agent name, counts by kind, last write, storage warnings |

Agent-writable kinds and their required fields: `convention` (category, rule), `decision`
(title, context), `bug_pattern` (name, signature), `alternative` (option, rejected_because),
`quirk` (description), `workaround` (solution, description), `refactoring_attempt` (outcome,
approach, reason_failed), `open_question` (question), `blocker` (description), `partial_work`
(work_type, location, state).

`context` is optional free text the agent already has (error output, a diff hunk, its goal).
Only its identifier-like terms are used to rank notes (`StripeSignatureError`, `cache_key`,
`E11000`, `ECONNRESET`); plain words and paths in it are ignored, because words such as
"error" or "failed" would match some note in almost any store and end abstention.

A failed call is a tool error whose text is JSON:
`{"error": {"code": "...", "message": "..."}}`, with `code` one of `invalid_argument`,
`unknown_kind`, `path_outside_repo`, `not_found`, `no_active_session`, `store_busy`,
`store_corrupt`, `too_large`, `queue_full`, `internal`.

### Lessons and review

An agent can leave lessons for a person to review, with `memory_capture_lessons` or the
`lessons` argument of `memory_session_end`. A lesson is `kind` (an agent-writable kind),
`fields` (the same fields `memory_remember` takes), `evidence` (required: why the agent
believes it), and `files` and/or `symbols` (at least one: what it is about; files are
anchored like any note's). Unknown keys, including `priority` and `confidence`, skip the
lesson. A lesson that fails these checks, or repeats a note or lesson already there, is
skipped with a reason and the others are still stored; `memory_session_end` ends the session
whatever capture does and reports it under `capture` when lessons were passed.

Limits: 10 lessons per call, 100 waiting at once (past that, `queue_full`: a person has to
review some first), and a lesson nobody reviews is archived after 30 days (on the next
capture, accept or reject; until then it is shown as expired and no longer counts toward
the 100).

A captured lesson waits for review (`pending`). It is never recalled, checked or carried to a
later session, no MCP tool returns its text, and the agent tools that change a note
(`memory_retract`, `memory_supersede`, `memory_feedback`, `memory_verify` and their `amem`
commands) refuse it. Only a person turns it into a note:

```bash
amem review list                  # what is waiting
amem review show ID               # one lesson in full, labelled as agent-written
amem review accept ID...          # shows each lesson, then asks you to type `accept`
amem review reject ID... --reason "wrong"   # kept for the record, never recalled
```

`accept` needs a real terminal for both input and output, asks per lesson, and has no flag,
environment variable or `--json` to skip the question; anything other than the word
`accept` skips that lesson, and Ctrl-C or end of input stops asking, keeps what was already
accepted and exits with code 2. There is no MCP tool that accepts or rejects. An accepted
lesson becomes an active note with source `reviewed`. `reject` needs no terminal. `show`
and the `accept` prompt print every further line of a multi-line value behind `    | `, so a
lesson's text cannot pass for a field, its evidence or another lesson; symbols with control
characters are refused at capture.

If a captured lesson repeats a note a person retracted or rejected before, it is still queued
but flagged: the capture result lists it under `rejected_before`, and `amem review list`,
`show` and the accept prompt say which note and when, so you can reject it quickly.

Agents without MCP use `amem capture`: a JSON list of lessons on stdin (`amem capture
--session ID`, `--json` for machine output).

```bash
echo '[{"kind": "quirk", "fields": {"description": "the retry queue drops jobs on restart"},
        "evidence": "saw it in worker.py while testing", "files": ["worker.py"]}]' | amem capture
```

`amem status`, `amem doctor`, `amem review list` and `memory_session_start` (as
`pending_review`) show how many lessons are waiting; `status` and `review list` also show how
many have expired but are not yet swept.

#### Upgrading

Every process that shares a store should run a build with lessons. A waiting lesson is stored
with status `archived` and a separate `review: pending` mark, so an older build reads it as
archived: its recall and MCP tools never return it. Its older commands (`amem check`, `amem
list`, `check_suggestion`, the session summary) still list it like any archived record, since
they never looked at status. A lesson a person accepts becomes an ordinary
active note that every build reads. In the other direction, a record whose status this build
does not know is read as archived (with a load warning), never as active.

### Remote server

Use it when an agent runs on someone else's machine and can only reach a URL (Base44 first;
Devin possibly). The server runs on a machine that has a checkout of the repository, because
file anchors are verified against that checkout exactly as over stdio. It serves the same
eleven tools over streamable HTTP at `/mcp`, behind bearer tokens.

```bash
pip install 'agent-memory[mcp]'                      # Python 3.10+; brings uvicorn and starlette

amem token create base44-prod --role read-write --url https://memory.example.com
amem token list
amem token revoke base44-prod

amem serve --http                                    # 127.0.0.1:8765, loopback only
amem serve --http --public-url https://memory.example.com --behind-tls
```

`amem token create` prints the token once (`amt_...`), the MCP server URL and a paste-ready
`Authorization` header; only a hash is kept, in `tokens.json` beside the store (override with
`--tokens PATH` or `$AGENT_MEMORY_TOKENS`). Only `amem install` gitignores the default
`<repo>/.agent-memory/tokens.json`: keep the token file untracked (add
`.agent-memory/tokens.json` to `.gitignore`) or put it outside the checkout with `--tokens`.
`--expires-in DAYS` limits a token's life. The
token's name is the agent name stamped on every note it writes, whatever the client calls
itself. The server refuses to start with no usable token (there is no anonymous mode), and
refuses a non-loopback `--host` without `--behind-tls`, which states that a TLS proxy or tunnel
fronts it. `--public-url` (it must look like `https://host[:port]`, or the server refuses to
start) adds that URL's host, lower-cased, to the hosts the server accepts. On a successful start
the server prints one line to stderr: the listen address, the token file, the number of usable
tokens and the allowed hosts (never a token).

| Role | Tools |
| --- | --- |
| `read-only` | `memory_recall`, `memory_check`, `memory_status` |
| `read-write` | all eleven |

A read-only token calling any other tool gets the tool error `forbidden`.

The server speaks plain HTTP, so put a TLS proxy or tunnel in front of it. Caddy:

```
memory.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Cloudflare Tunnel (a quick tunnel; use a named tunnel for a stable hostname):

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

With a proxy or tunnel, pass the public hostname with `--public-url`: a request whose Host is
not loopback or that URL's host gets 421, and a request with an Origin header gets 403.

**Base44:** add a custom MCP server by URL, `https://memory.example.com/mcp`, with the header
`Authorization: Bearer <token>`. **Devin:** unverified; the same URL and header.

**Audit log.** One line per event on stderr (`<time>` is UTC, `...Z`): `<time> call name=<token name> tool=<tool>
outcome=<ok or an error code> ms=<duration>`, and `<time> auth denied reason=<missing|invalid>`
(with `throttled=yes` when the failure limiter answered). It never holds an argument, a result
or a token. A wrong, missing, expired or revoked token gets the same 401 with a fixed body.

**What a remote agent cannot do.** Accept, reject or promote a lesson (`memory_capture_lessons`
and the `lessons` argument queue them as pending; `amem review accept` stays on the machine
with the store), set `priority: must`, choose the repository or the store, or see the server's
paths (`memory_status` shows the store's file name and the repository's directory name only).
Request bodies over 1 MB get 413, and a bearer value over 512 characters is refused as invalid.

**Known limits.**

- Tokens are checked at request time. Revoking one stops it at the next request; its service
  closes at the next call after a clean read of the token file. A revoke during an in-flight
  request can leave a service open for the revoked token until the next token-file change or
  shutdown (the guard still refuses the token).
- The failure limiter is global, not per address: 20 failed authentications in 60 seconds make
  further invalid tokens get 429. A valid token always passes.
- The token file mode (0600) is best effort on Windows; keep the file in a private directory.
- One store and one repository per server, fixed at start-up.
- Plain HTTP only: a TLS proxy or tunnel is required off loopback.
- `created_at` has one-second resolution: revoking and re-creating a token with the same name
  and role within one second reuses the old service.
- `--public-url` is validated and its host lower-cased; a trailing dot or a Host header in upper
  case is not normalised and is refused with 421.
- Calls are served one at a time; a slow call (for example a store-lock wait of up to 10 s while
  another process checkpoints) delays all callers, including authentication of new requests.
- Every throttled request still writes one WARNING line to the audit log.
- Reaching a server from a different `amem`: if `amem token` or `amem serve --http` is not
  found, the `amem` on your PATH may come from another checkout than the one in front of you.
  Run `python -m agent_memory.cli ...` from the repository, or reinstall with `pip install -e .`.

### The same through the command line

The memory commands (`recall`, `guard`, `remember`, `feedback`, `retract`, `verify`,
`status`, `session start|end`) take `--json` for machine-readable output (an error is
`{"error": {"code", "message"}}` with exit code 2), and `--store`, `--repo`, `--agent` and
`--allow-must`. `amem install` and `amem doctor` take `--json`, `--repo` and `--store` (no
`--agent`):

```bash
amem session start "add retry logic to the payment webhook"       # prints the session id
amem recall "add retry logic to the payment webhook" -f webhooks/payment.py --json
amem recall "fix the failing webhook test" --context "StripeSignatureError: no signatures found"
pytest 2>&1 | tail -5 | amem recall "fix the failing test" --context -   # at most 2000 chars
amem guard "retry the webhook forever with backoff" -f webhooks/payment.py
amem remember decision -F title="Cap webhook retries at 5" -F context="INC-2026-03" \
    -f webhooks/payment.py --session <session-id>
amem feedback <id> --helpful        # or --harmful
amem retract <id> --reason "the code changed"
amem verify <id>
amem status
amem session end <session-id> --summary "retries capped"
```

`amem guard` is the command-line `memory_check` (the name `amem check` already belongs to an
older command that is kept). `amem capture` and `amem review` are described under "Lessons and
review".

### Where the store lives, and who is writing

The store is `<repo>/.agent-memory/project.memory`, where the repository root is the nearest
directory with a `.git` at or above the working directory. Override with `--store` /
`AGENT_MEMORY_STORE` and `--repo` / `AGENT_MEMORY_REPO`; the server fixes both when it starts,
and no tool argument can change them. Writes are stamped with the MCP client's reported name
(`claude-code`, `cursor`, ...), else `--agent` / `AGENT_MEMORY_AGENT`, else `cli`.

Recalled notes come back inside a labelled block,
`<recorded-project-notes untrusted="true">`, that says they are data written by earlier agents
and not instructions. Every `<` inside a note is escaped as `&lt;` (after folding look-alike
characters), so a note cannot close the block or open a tag of its own, and terminal control
characters are dropped.

What an agent cannot do through the tools: delete anything (there is no delete; notes are
retracted or superseded and kept), choose or change the store, name a file outside the
repository (`..`, absolute paths elsewhere and symlinks that leave it are refused), set the
author, session, status, priority or other bookkeeping fields through `fields`, or pin a
`must` note (priority `must` is refused unless the server was started with `--allow-must`, a
human decision).

### Check the setup

```bash
amem doctor            # one line per check; exit 0 when all are ok, else 1
amem doctor --json
```

It reports the Python version, whether the `mcp` package is installed (only a note when it is
not: just `amem serve` needs it), the repository root, the store path, and, when the store
exists, whether it opens, how many journal transactions are not yet folded into the snapshot,
any storage warnings, which of the `.lock` and `.journal` files are present, and for each tool
whether its instruction block (and, for Claude Code and Cursor, its `.mcp.json` entry) is
installed. For `codex` and `devin` it can see only the `AGENTS.md` block: their MCP
registration lives in the tool's own settings. Because `codex`, `devin` and `generic` share
one `AGENTS.md` block, installing any one of them makes the doctor report all three as
installed. A store whose `.journal` survives without its snapshot is reported as
`store_corrupt` (restore the snapshot; do not delete the journal), and a check that fails
unexpectedly is reported as that one check failing, never as a traceback. The doctor never creates a store, and it reads
a plain `.memory` file without converting it to a shared one.

### Known limits

- The stdio server talks to a client on the same machine; hosted agents that need an MCP
  server by URL (Base44) use the remote server (see "Remote server"). Local filesystems only (no NFS or SMB), and the store's lock is not fair: a
  process writing in a tight loop can make another wait until `store_busy`.
- Tool support for Devin, Codex and Cursor beyond the files written here comes from secondary
  sources and is unverified; Replit Agent's MCP support is unknown, so use the `generic`
  install and the `amem` commands there.
- Agents may simply not call the tools. The instruction block and short tool descriptions are
  the mitigation, not a guarantee.
- A recall that finds nothing ranks twice (once more, without a budget, to tell "nothing
  relevant" from "relevant notes did not fit"), so an empty recall costs about two recalls.
- FastMCP puts `Error executing tool <name>:` and a space in front of a tool error's JSON
  text, so a client parses the JSON after that prefix. An argument of the wrong type is
  rejected by the MCP layer itself, with no stable `code`.
- With `--json`, a usage error (a missing argument, an unknown option) is still click's plain
  text on stderr with exit code 2, not JSON.
- The legacy write commands (`amem add ...`, `start-session`, `end-session`, `consolidate`)
  now use `open()`: on a plain `.memory` file they convert it in place, one way, to a shared
  store (a `.journal` and `.lock` appear beside it), after which plain `load()` still works
  but `save()` to that path is refused. To return it to a plain file, see "Sharing one
  memory between agents" below. An `.isonl` file cannot be a shared store: it works only with
  the legacy commands and plain `load()`/`save()`; `amem serve` and the commands above refuse
  it.
- A relative `--store` in an MCP config is resolved against the working directory the client
  starts the server in, which varies between clients; prefer the default or an absolute path.
- The MCP server keeps one current session per server process. Over stdio that is one client,
  so it is the client's session; a server shared by several clients (the remote transport,
  not built) would need one per connection.
- In CLI mode the guards are advisory. `--allow-must`, `--agent` and `--store` are ordinary
  flags that an agent running `amem` commands can pass itself, so the `must` gate, the
  recorded author and the store choice hold only against an agent that cannot change its own
  command line. Only the MCP server's configured `--allow-must` is enforced, against an agent
  that cannot alter the server's arguments.
- The read commands (`amem status`, `recall`, `guard`) only read. A store that does not exist
  is not created (nothing appears, not even `.agent-memory/`): `recall` and `guard` answer as
  for an empty store, and `status` succeeds with `"store_exists": false`. An existing store is
  read as it is on disk (the snapshot with the journal replayed, holding the store's lock only
  while reading, as plain `load()` does), never opened, converted or checkpointed: a plain
  `.memory` file stays plain and a journal is left as it was. Each command reads afresh, so it
  sees every write a running server has acknowledged. The write commands and the MCP server
  still open the store (creating it on first use) and checkpoint when they close.
- `quirk`, `bug_pattern`, `alternative` and `refactoring_attempt` are guardrail kinds. A note
  of these kinds from any agent is shown under "## Guardrails" and makes `memory_check` /
  `amem guard` warn (`warn: true`), still inside the untrusted block. Such a note is marked
  unreviewed: every recall and check item carries `unreviewed` (true for a guardrail-tier note
  whose source is `explicit` or `extracted`, false otherwise), a check that returns one adds a
  `note` saying the warnings come from agents and were not reviewed by a human, and the human
  output of `amem guard` / `amem recall` shows `[unreviewed]` after its id. The flag means
  "a guardrail no person has confirmed": the legacy `amem add bug-pattern` and similar
  commands stamp `explicit` too, so a person's notes added that way are marked unreviewed as
  well (see the next bullet for how a note gets cleared).
- Accepted lessons are stamped `reviewed`, and that is the only thing that clears
  `unreviewed`: `amem review accept` is the human-review mechanism. Legacy `amem add` notes
  still show as unreviewed.
- The terminal check on `amem review accept` is a strong barrier, not proof: a program that
  drives a pseudo-terminal could pass it. The design relies on there being no MCP route, no
  flag and a confirmation a person can see.
- A lesson's evidence is checked for presence and for its file or symbol anchors, not for
  truth; the person reviewing is the check.
- The legacy commands and store methods (`amem check`, `amem list`, and helpers such as
  `decisions.check_decision`, `conventions.get_conventions_for_file` and
  `sessions.get_session_context`) leave waiting lessons out, but they still show retracted
  and superseded records, as they always did; `recall`, `guard`, `get_context` and the MCP
  tools do not.
- Small known rough edges: `amem capture` on a terminal waits for stdin without a prompt; and
  accepting or rejecting an unknown id in a repository with no store creates an empty store.

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
  before, with these differences:
  - `save()` is atomic: it writes a temp file beside the target and replaces the target with
    it. Through a symlink it writes the link's target and keeps the link, and on POSIX it
    keeps the file's permission bits, as before. A new file does replace the old one, though:
    a hard link to it keeps the old content, and on Windows attributes or permissions set on
    the file itself are not carried over. When the file cannot be replaced (on Windows,
    another program has it open), `save()` retries for up to 2 s before raising.
  - `save()` writes LF line endings (on Windows it used to write CRLF, so a saved file
    changes its bytes once).
  - Ids built from a per-process counter (sessions, tasks, goals, issues, issue comments,
    learning events) skip a number that is already taken in the memory; they used to raise
    `DuplicateNodeError`, for example in a fresh process working on a loaded file.
  - `load()` gives `memory.graph` as a `SharedGraph`, a subclass of `ISONGraph` that behaves
    like it when no shared store is attached.

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
`checkpoint()` and `close()`. If the snapshot cannot be replaced (on Windows, another program
has it open), the compaction is skipped with one warning in `memory.warnings`, and
automatic compaction is tried again once the journal has doubled or 60 s have passed;
`checkpoint()` and `close()` always try. `AgentMemory.load()` on a shared store gives a plain,
single-process copy with the journal replayed; it takes the store's lock to read it, so it can
raise `StoreLockTimeout`, and it creates `<path>.lock` if it is missing.

The store is identified by its real path: symlinked names of one file (for example the same
memory linked into several git worktrees) share one journal and one lock, beside the real
file. Two hard links to one file are two separate stores; do not share a store that way.
`open()` refuses a path ending in `.isonl` with `ValueError`, before touching any file: a
shared store keeps its snapshot in ISON text. Plain `load()`/`save()` of `.isonl` work as
before.

If a call raises `StoreCorrupt`, the store's files are damaged (edited by hand, a disk
error), and the message names the file and the byte or line. Nothing is repaired or guessed
and nothing is rewritten. Restore the snapshot from a backup (or the last checkpointed copy),
or inspect the journal at the named line; do not delete the journal to make the error go
away, since it holds every write since the last checkpoint. A journal without its snapshot
is reported the same way.

Limits:

- Local filesystems only. Network filesystems (NFS, SMB) are not supported: OS file locks
  are not reliable there. FAT and exFAT are not supported either (coarse timestamps,
  unstable file ids).
- The lock is not fair: a process writing in a tight loop can starve others, which then
  raise `StoreLockTimeout` after `lock_timeout`.
- Writes hold the lock while they scan the records of their kind for a duplicate (about
  20 ms per `remember` at 5,000 records), and `end_session()` holds it for the whole
  consolidation scan, so other processes wait that long.
- One `AgentMemory` instance must not be used from several threads at once; give each thread
  its own instance, or serialise the calls.
- Code that iterates `memory.graph.nodes()` or `edges()` while calling store methods should
  iterate a copy (`list(memory.graph.nodes())`): a store call can apply other processes'
  writes to the graph mid-loop.
- After `close()`, store methods and writes raise `ValueError("this store is closed")`;
  `recall()` and `check()` answer from the state the instance last saw.
- `close()` never raises because the lock is busy: if it cannot take the lock within
  `lock_timeout`, it skips its checkpoint (every write is already in the journal) and records
  a note in `memory.warnings`.
  Call `checkpoint()` yourself when you need to know the snapshot is current (it returns
  `True` when it folded the journal).
- `export_json()` and `save()` to another path include the internal `store_meta` node.
- A store stays shared after every process has closed it: once a path has been opened with
  `open()`, its `.journal` file remains, and a `save()` to that path from any other memory
  (from `load()`, from `AgentMemory(...)` or from another shared store) raises `StoreInUse`.
  This is
  deliberate: a plain save over a store that has a journal can write a snapshot whose
  recorded sequence number is stale relative to its content, so the next opener would
  re-apply journal operations. `load()` and the CLI's read commands keep working (`load()`
  replays the journal). The `amem` write commands use `open()` themselves, so they work on a
  shared store, and on a plain `.memory` file they make it one (see "Known limits" above).

To turn a shared store back into a plain file: make sure no other process is using it, then
open it once, checkpoint, close, and delete the journal. After that plain `load()`/`save()`
work again.

```python
import os
from agent_memory import AgentMemory

path = "project.memory"
memory = AgentMemory.open(path)   # replays whatever is left in the journal
if not memory.checkpoint():       # folds all of it into the snapshot
    raise RuntimeError(f"checkpoint skipped ({memory.warnings}); keep the journal")
memory.close()
os.remove(path + ".journal")
```

The explicit `checkpoint()` matters: `close()` checkpoints only when that instance wrote
something, so open-and-close alone can leave journal content outside the snapshot (for
example the writes of a process that crashed without closing). Deleting the journal without
the checkpoint can lose acknowledged writes. Leave `<path>.lock` where it is: plain mode
ignores it, and deleting it while some process still holds it would let a new process take
the lock beside it on POSIX.

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

`PYTHONPATH=src python -m tests.eval.run`, run from the repository root, prints the
retrieval scorecard (in PowerShell: `$env:PYTHONPATH="src"; python -m tests.eval.run`): 50
scenarios over a small synthetic project (about forty records), judged on the
rendered text for must-include notes, superseded or retracted notes leaking
through, and abstention (nothing returned for an unrelated task). Run it with
`PYTHONPATH=src` so the checkout, not an installed copy, is measured. Current
figures: must-include recall 1.00 (30/30), leaks 0, abstention 1.00 (12/12).
That abstention figure is in-sample (the scenarios were used while tuning); on
16 held-out tasks never used for tuning it is **0.38 (6/16)**, a known weak
spot: a single shared word can let an unrelated, labelled note through.
`--service` runs the same scenarios through `MemoryService` (the boundary the
MCP server and the CLI use; the prompt judged is the untrusted block) and gives
the identical scorecard; `--shared` runs them on a shared store. The baseline
and the current results are recorded in `tests/eval/BASELINE.md` and
`tests/eval/RESULTS.md`.

The strict latency gates are opt-in: `python -m pytest tests -m perf`. One of
them is recall through the service at 5,000 records, which must have a p95
under 50 ms (`tests/test_service_perf.py`). **That gate currently fails on the
development laptop.** These are measured numbers, not a promise. At full clock
the p95 is about 44-45 ms (median 36-42 ms). Once the laptop throttles under
sustained load, the p95 rises to about 86-88 ms (100 ms in the
test's own run). The remaining cost is the engine's ranking over thousands of
candidates. The run takes several minutes, because the 5,000-record tests
first build their stores. The default run keeps loose always-on guards that
catch only a pathological slowdown.

The installer's golden files (`tests/golden/install/`) are regenerated with
`UPDATE_GOLDEN=1 python -m pytest tests/test_installer_tools.py -q -k golden`;
read every changed file before committing it.

## License

MIT
