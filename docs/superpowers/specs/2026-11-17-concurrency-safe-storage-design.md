# Concurrency-Safe Storage — Design (sub-project 2)

Status: draft for review · Date: 2026-11-17 · Builds on: `2026-11-16-retrieval-and-record-model-design.md`

## 1. Purpose

Several agents (Claude Code, Codex, Cursor, a CLI, an IDE plugin) will work on one repo at
the same time and share one memory file. Today that loses data. This sub-project makes the
shared memory **safe for many processes on one machine**, and leaves a clean seam for a
single-owner server (sub-project 3's MCP server) — which is just another client of the same
store.

What a person using it should be able to rely on:

- A write that returned is durable and visible to every other process on its next read.
- Two agents writing at the same moment never lose each other's writes and never produce a
  duplicate of the same fact.
- Killing a process at any instant (including mid-write) never corrupts the memory.
- A reader never sees a half-written file.
- Single-process use keeps working exactly as before, with no new required steps.

### Out of scope

- Network filesystems (NFS/SMB) and cross-machine sharing — OS file locks are not reliable
  there. Documented as unsupported.
- Git-based sharing / record-level merge between branches (scenario 2; a later spec).
- The server/MCP surface itself (sub-project 3). This spec only guarantees that one
  long-lived process and several short-lived ones can share a store safely.
- `GovernedAgentMemory`, `greenfield`, `integration` internals. They hold an `AgentMemory`
  and call `save()`, so they gain the atomic `save()` below and nothing else.
- Per-process state: semantic-search index, `writer.session_id`, caches. Unchanged.

## 2. What breaks today (measured, not assumed)

1. `ISONGraph.save` is `Path.write_text`: not atomic. A crash leaves a truncated file; a
   concurrent reader can load half a file.
2. `save()` rewrites the whole file from one process's memory image, so two processes that
   each loaded, added a record and saved lose one record (last writer wins).
3. Check-then-act logic (merge on same text, natural-key supersede, counters such as
   `helpful`, `usage_count`, `examples_seen`) is computed on a stale image.
4. On Windows `os.replace` over a file that another process has open for reading raises
   `PermissionError` (verified on this machine), so a naive "write temp, rename" is not
   enough; and `msvcrt.locking` has no shared mode, so a reader/writer lock is not available
   from the standard library.

## 3. Approaches considered

**A. Lock, reload, merge, rewrite (optimistic).** Every save takes a lock, re-reads the
file, three-way merges, rewrites everything. Write cost is O(file); needs a per-field merge
rule for every kind; counters and removals need special cases. Rejected.

**B. Snapshot + append-only operation journal (recommended).** The existing `.memory` file
stays the snapshot. Every mutation is appended as one checksummed line to a journal under a
short exclusive lock; readers replay the journal tail; the journal is folded into a new
snapshot now and then. Write cost is O(1); history is a serial order, so no merge rules are
needed; a crash can only lose an unacknowledged tail; and it is exactly what a server
process also wants.

**C. SQLite as the store.** Strong concurrency for free, but the ISON text file stops being
the artifact, every store and the retrieval caches would need re-plumbing, and it throws
away the point of building on ISONGraph. Rejected for now; the backend seam in §6 keeps it
possible later.

Recommendation: **B**.

## 4. Design

### 4.1 Files

For a store at `project.memory`:

| File | Content |
|---|---|
| `project.memory` | The snapshot. Valid ISON, readable by today's `load()`. Contains one extra node (type `store_meta`, id `journal`) carrying `generation` (random id) and `seq` (last journal op folded in). |
| `project.memory.journal` | UTF-8 lines. Line 1 is a header `{"journal":1,"generation":"<id>"}`. Each later line is one **transaction**: `<crc32 hex8> <json>\n`, json = `{"seq":n,"actor":"<agent>","ts":"<utc>","ops":[...]}`. |
| `project.memory.lock` | Empty. The OS lock target. |

Ops are `["n",type,id,props]` add node, `["u",type,id,props]` update (absolute values),
`["e",rel,src,dst,props]` add edge, `["rn",type,id]` remove node, `["re",rel,src,dst]` remove
edge. One line per transaction means a transaction is all-or-nothing on disk.

The `store_meta` node lives in the snapshot, so generation and sequence change **atomically
with the snapshot**; no second file has to agree with it.

### 4.2 Locking

One exclusive lock on `project.memory.lock`: `msvcrt.locking` on Windows, `fcntl.flock`
elsewhere, plus a per-process `threading.RLock` (two threads of one process must not
contend through the OS). The OS releases the lock when a process dies, so a crashed writer
never leaves a stale lock. Acquisition has a timeout (default 10 s) raising
`StoreLockTimeout`. There is no shared mode; snapshot reads happen only at open and after a
compaction, so contention is negligible.

### 4.3 Transactions — the core idea

`graph.transaction()` is re-entrant. The outermost entry: take the lock, **refresh** (apply
every journal line past our offset), run the body, append one line with the ops the body
recorded, flush (fsync if configured), release.

Because every mutation reads the latest state under the lock, the journal is a serial
history and **absolute values are safe** — no deltas, no merge rules. Two writers with the
same fact: the second sees the first's record inside its own transaction and the existing
merge-on-same-text rule reinforces it instead of duplicating.

`SharedGraph` (a subclass of `ISONGraph`) overrides the five mutators. Called outside a
transaction, each wraps itself in an implicit one. Called inside, it just records. Replay
of other processes' ops goes through `ISONGraph.<method>(self, …)` so it applies without
re-recording, and the retrieval caches, which validate against the graph, see it.

Explicit transactions wrap: every `MemoryWriter` mutating method, `anchors.add_anchor` /
`restamp`, session start/end, consolidation, and the ~4 store sites that derive a value from
an earlier read (`skills.examples_seen`, `templates.usage_count`, and the writer's
`feedback` / `_reinforce` / evidence append). Task 1 of the plan is an **audit** of all 49
`update_node` call sites against this rule. Plain `ISONGraph` gets a no-op `transaction()`
through a helper, so store code does not care which mode it is in.

If the body raises, the ops already applied are **still appended**, then the error
propagates. Memory and journal must never disagree, and that matches today's no-rollback
behaviour.

### 4.4 Reads and freshness

`recall`, `check`, `get_context` call `refresh_if_stale()` first: one `stat` of the journal
(size and mtime) against the stored offset; only when it changed, read and apply the tail,
**without** taking the lock. A partial last line fails its checksum and is ignored until it
completes. An unchanged store costs microseconds. A generation mismatch means a compaction
happened: reload the snapshot (under the lock) and continue.

### 4.5 Compaction

Triggered by the writer that crosses the threshold (journal larger than 1 MiB **and** larger
than the snapshot), by `checkpoint()`, and by `close()`. Under the lock: write the snapshot
(new generation, `seq` = last op) to a temp file in the same directory, `fsync`, `os.replace`
with bounded retry and backoff (default 2 s) to ride out Windows `PermissionError`, then
rewrite the journal with the new header. Crash between the replace and the journal rewrite
is safe: the journal's generation no longer matches the snapshot's, so it is known to be
already folded in and is discarded. If the replace never succeeds (an antivirus or editor
holds the file), compaction is skipped and the journal keeps growing; that is a warning, not
an error.

Readers read the whole snapshot and close it immediately and never keep the file open.

### 4.6 Torn tails and recovery

A crash mid-append leaves a last line without a valid checksum. Readers ignore it. The next
writer, holding the lock, truncates the journal to the last valid line before appending. A
stale temp snapshot from a crashed compaction is deleted on the next open. If a *middle*
line fails its checksum the store is corrupt: raise `StoreCorrupt` naming the line, never
guess. Replaying an op that is already applied (duplicate node/edge) is idempotent and adds a
warning to `load_warnings`; it cannot arise from the protocol and indicates outside editing.

### 4.7 Durability

`durability="flush"` (default): the journal line is flushed to the OS before the call
returns, so it survives the **process** crashing. `"fsync"`: also survives power loss, at
the cost of a disk sync per transaction. Compaction always fsyncs.

## 5. API

```python
mem = AgentMemory.open("project.memory", repo_root=".", agent="claude-code")   # shared mode
mem.remember(...); mem.recall(...)       # durable on return; recall sees other processes
mem.refresh() -> int                     # ops applied (also automatic before reads)
mem.checkpoint()                         # fold the journal into the snapshot
mem.close()                              # checkpoint + release; also a context manager
```

`open()` creates the store if missing. Everything else in the public API is unchanged.

- `AgentMemory.load(path)` still works. If a journal exists beside the file with a matching
  generation it is replayed, so data is never silently missed.
- `AgentMemory.save(path)` is now **atomic** for everyone (temp file, fsync, replace with
  retry). In shared mode, saving to the store's own path is `checkpoint()`; saving to
  another path writes a full snapshot copy. A plain (non-shared) instance saving over a path
  that has a live journal raises `StoreInUse` pointing at `open()`.
- New exceptions: `StoreLockTimeout`, `StoreCorrupt`, `StoreInUse`, all `AgentMemoryError`.

## 6. Seam for the server (scenario 3)

The persistence logic lives behind a small `Backend` protocol: `transaction()`, `refresh()`,
`checkpoint()`, `close()`. `JournalBackend` is the implementation. A server (sub-project 3)
holds one long-lived `AgentMemory.open(...)`; CLI calls from other processes keep working
against the same files because the server is just another journal client. A future SQLite or
remote backend replaces `JournalBackend` without touching stores or retrieval.

## 7. Behaviour decisions I made (change any of them)

1. **Journal over merge** (§3). Cost if wrong: replacing the backend, which the seam allows.
2. **Opt-in `open()`**; `load`/`save` stay as they are. Cost: users must adopt `open()` for
   sharing. Safer than silently changing semantics.
3. **One exclusive lock, no shared mode**, because the standard library has none on Windows.
   Cost: snapshot reads briefly serialise.
4. **Default `durability="flush"`.** Survives process death, not power loss. Cost: the last
   write can be lost on power failure; opt in to `"fsync"`.
5. **Commit partial ops when a transaction body raises**, rather than roll back. Cost: a
   half-finished compound call leaves its first half, exactly as today.
6. **`store_meta` node inside the snapshot** for atomic generation. Cost: `get_stats()` node
   counts include one extra node in shared mode (excluded from the stats).
7. **Compaction inline** in the writer that crosses the threshold. Cost: that one write is
   slower (tens of ms for thousands of records).
8. **No network filesystems.** Cost: documented limitation.

## 8. Failure modes

| Event | Result |
|---|---|
| Process killed mid-append | Unacknowledged transaction lost; store intact (§4.6) |
| Process killed mid-compaction | Old snapshot or new snapshot, never a mix; journal reconciled by generation |
| Lock holder dies | OS releases the lock; no stale lock |
| Lock held longer than timeout | `StoreLockTimeout` (the caller decides to retry) |
| Windows file held open by a third party during replace | Retry, then skip compaction with a warning |
| Journal edited by hand mid-file | `StoreCorrupt`, naming the line |
| Disk full on append | Error raised before acknowledgement; the process then re-syncs its in-memory graph from disk, which discards the change that never reached the journal |

## 9. How we will know it works

Everything below is a test, and the stress and crash tests are the acceptance gate:

1. **Multi-process stress.** 8 processes × 200 mixed operations (remember, feedback,
   supersede, anchor, session start/end), including the *same* fact written by several at
   once. Pass: no exception, every acknowledged write present, no duplicate of any fact,
   final state equal to a fresh replay of the journal, every line checksum-valid.
2. **Crash injection.** A child exits abruptly (`os._exit`) at named points: before append,
   mid-line, after append, mid-compaction, between replace and journal rewrite. Pass: reopen
   yields a state that contains every acknowledged write and is a prefix-consistent history;
   no truncated snapshot ever.
3. **Torn tail** and **corrupt middle line** handled as in §4.6.
4. **Readers during compaction** in a loop never see a partial store or an exception.
5. **Counters under contention.** N processes each call `feedback` / template use M times;
   total is exactly N×M.
6. **Serial equivalence.** The full existing test suite passes unchanged, and the evaluation
   suite run through `open()` produces results identical to the in-memory run.
7. **Cache visibility.** A record written in process A is returned by `recall` in process B
   without any explicit call.
8. **Performance** (opt-in `pytest -m perf`, plus a loose always-on guard): write p95 under
   15 ms with 4 contending processes, no-op `refresh_if_stale` under 0.2 ms, open of a
   5,000-record store under 1.5 s.

## 10. Risks that remain

- `ISONGraph.from_ison` must construct the subclass. Verified in Task 1; if it does not, a
  small factory replaces it. Nothing else in the design depends on this.
- The audit of write sites can miss a read-modify-write. Test 5 and a generated concurrency
  test that calls every public store mutator from two processes are the net.
- Sessions: `start_session` links to the "last session" of *any* agent. With several agents
  that is arguably right (shared memory) but it is a behaviour change worth knowing about; it
  is not altered here.
- Power-loss durability is opt-in (§4.7).

## 11. Plan shape (for the next step)

About 10 tasks: (0) audit + `from_ison` check + atomic `save()`; (1) lock + journal codec;
(2) `SharedGraph` recording/replay; (3) `JournalBackend` open/refresh/transaction;
(4) compaction + recovery; (5) writer/anchors/sessions/consolidation transactions;
(6) counter-site fixes; (7) `AgentMemory.open`/`load`/`save` integration + exceptions;
(8) multi-process stress + crash-injection harness; (9) perf gates, docs, README/DESCRIBE.
Each task: implementer + reviewer subagents, as before.

## 12. Implementation notes

Written after the nine tasks were built and reviewed. The design above held; these are the
places where the code differs from it or adds to it, and what the final checks found.

### 12.1 Deviations from the design

- **Every public store method is a transaction** (§4.3 named about four sites).
  `BaseStore.__init_subclass__` wraps every public, non-generator method of every store in
  `transactional`, so a store read takes the lock, applies other processes' writes first and
  sees fresh state, and no read-modify-write in a store can race. The audit could therefore
  not miss a site. The cost is that store reads take the lock: about 0.1 ms per call
  uncontended (0.12 ms measured for `conventions.get_stats()` on the development laptop).
  The facade's `recall`, `check` and `get_context` stay lockless (`refresh_if_stale`), and
  `AgentMemory.get_stats()` runs as one transaction, so it takes the lock once instead of
  once per store and returns numbers from a single state.
- **The writer's text index follows remote changes.** `SharedGraph.remote_ops` is bumped
  whenever content arrives from another process (a replayed op, a reload from disk); the
  `MemoryWriter` drops its merge-on-same-text index when the counter moved, so a fact written
  by another process is merged, not duplicated.
- **`JournalBackend.read_only(path)`** returns the snapshot with the journal replayed as a
  plain graph with no backend. `AgentMemory.load()` uses it when a journal exists.
- **Lockless reads never declare corruption.** Any anomaly in a lockless read (another
  generation, a sequence gap, a damaged or vanished line, a shrunk or unreadable file) makes
  it retry under the lock, and only there is `StoreCorrupt` raised, when the journal read from
  its start is damaged.
- **An unreadable journal is never treated as absent.** `journal.Tail.error` carries the
  reason (a sharing violation, a directory, an I/O error, or a first line that is complete
  but not a header); under the lock it raises `StoreCorrupt`, and a lockless read retries
  under the lock. A journal counts as absent only when it is missing, empty, or a single
  unterminated line (a crash while the header was written); a damaged header with lines
  after it is never reset over, and `load()` raises too.
- **A journal without its snapshot is refused.** When the snapshot is missing and the journal
  holds a transaction (or cannot be read), `open()` raises `StoreCorrupt` and touches nothing;
  for a store never compacted, the journal held every write. An empty or header-only journal
  beside a missing snapshot still gives a fresh store.
- **A store is keyed by its real path.** `JournalBackend` resolves the path with
  `os.path.realpath`, so symlinked names of one snapshot share one journal and one lock
  beside the real file. `atomic_write_text` writes through a symlink to its target and, on
  POSIX, copies the existing target's permission bits onto the temp file before writing to
  it (Windows chmod only sets the read-only flag, so it is not copied there). The graph keeps
  the name the caller gave.
- **`.isonl` paths are refused by `open()`** (`ValueError`, raised before any file is
  touched, for the given name and for the resolved one): the snapshot is ISON text, and plain
  `load()` reads an `.isonl` path as ISON lines. Plain `load()`/`save()` of `.isonl` are
  unchanged.
- **Automatic compaction backs off after a skip.** When the snapshot cannot be replaced, the
  writer records the attempt and does not try again automatically until the journal has
  doubled or 60 s have passed; `checkpoint()` and `close()` always try. The skip is warned
  once per run of skips, so `warnings` stays bounded. Without this, every commit past the
  threshold paid the 2 s replace retry inside the lock.
- **A live writer follows a moved snapshot.** A compaction that replaced the snapshot and then
  crashed (or failed) before rewriting the journal leaves a journal that still carries the old
  generation. A process that was behind it must not append to that stale journal, because the
  next reader discards it. Under the lock, every transaction first compares the snapshot's
  file signature `(size, mtime_ns, inode)` with the one it is based on; when it changed, the
  snapshot is read and its generation decides: the same generation continues, another one
  makes the process rebuild from the new snapshot (and reset the journal) before it appends.
  The same signature is part of what `refresh_if_stale` compares, because a rewritten journal
  can have the old size and mtime.
- **A per-backend thread lock.** Each `JournalBackend` has an `RLock` taken before the file
  lock and around lockless reads, so threads of one process do not contend through the OS
  and never interleave on the graph or the journal position.
- **`checkpoint()` is refused inside a transaction** (`RuntimeError`): the transaction's ops
  are not journaled yet, so folding the graph then would put unjournaled state in a snapshot.
- **`close()` does not raise on a lock timeout.** Every write is already in the journal, so a
  checkpoint at close that cannot take the lock in time is skipped and a note is appended to
  the backend's `warnings`; the next compaction folds the journal instead.
- **Stale temp snapshots are removed only when older than 60 s** (by `open()`), so the temp
  file of an atomic write still in progress outside the store's lock, such as a plain
  `save()` to the same path, is not deleted under it.

### 12.2 Behaviour users can observe

- `save()` writes LF newlines (it is now atomic and writes the text itself). On Windows it
  used to write CRLF, so a saved file changes its bytes once.
- A snapshot deleted while agents run makes every locked operation of a live instance raise
  `StoreCorrupt`: loud, never silent loss. Lockless reads keep answering from memory.
- After `close()`, store methods and writes raise `ValueError("this store is closed")`;
  `recall()` and `check()` keep answering from the state the instance last saw.
- One `AgentMemory` instance is not safe for use from several threads.
- Code that iterates `graph.nodes()` or `graph.edges()` while calling store methods can hit
  "dictionary changed size during iteration" in shared mode, because each store call can
  apply other processes' writes; iterate a list copy (as `check_suggestion` now does).
- In shared mode `export_json()` and `save()` to another path include the internal
  `store_meta` node.
- Two processes that start a session in the same second get distinct session ids, and the
  same holds for every id built from a per-process counter (tasks, goals, issues, issue
  comments, learning events): `BaseStore._counter_id` bumps the counter while the id is taken,
  inside the store method's transaction. Without a collision the id is unchanged; in plain
  mode a collision (a fresh process on a loaded file) now gets the next number instead of
  raising `DuplicateNodeError`.
- A `save()` to a path that has a journal raises `StoreInUse` from any instance whose own
  store it is not, shared ones included; before, a shared instance could overwrite another
  live store, whose next opener then reset its journal.
- `load()` of a shared store takes the store's lock to read it, so it can raise
  `StoreLockTimeout`, and it creates `<path>.lock` when missing.
- `SharedGraph.node_count(node_type)` keeps `ISONGraph`'s optional type; only the untyped
  count leaves out the `store_meta` node. `load()` returns its graph as a `SharedGraph`.

### 12.3 Limits

- Local filesystems only: no NFS or SMB. FAT and exFAT are not supported either: their
  timestamps are coarse and their file ids unstable, and the snapshot signature relies on
  both.
- The file lock is not fair. A process writing in a tight loop can starve others, which raise
  `StoreLockTimeout` after the default 10 s.
- `durability="flush"` survives the process dying, not a power loss; `"fsync"` adds a disk
  sync per transaction.
- The strict performance gates are opt-in (`pytest -m perf`) because they fail on a
  throttled laptop; two loose guards always run.
- A lockless reader that read a journal line which was later rolled back (its append failed
  and was cut off) and replaced by a different transaction of exactly the same byte length
  cannot tell the two apart and diverges from disk. It needs a failed flush or fsync; every
  other length is detected and rebuilt from disk.
- Two hard links to one snapshot are two stores (each with its own journal and lock), and a
  compaction through one name detaches the other. Symlinks are resolved (12.1).
- A damaged byte inside the header's generation id still parses as a header, of another
  generation, and is taken for a stale journal: its lines are discarded as already folded in.
  Only a header that no longer parses is reported.
- Writes hold the lock while they scan the records of their kind (about 20 ms per `remember`
  at 5,000 records), and `end_session()` holds it for the whole consolidation scan.

### 12.4 What the final checks found (Task 9)

- **Serial equivalence.** With `AGENT_MEMORY_FORCE_SHARED=1` every `AgentMemory(...)` in the
  suite is backed by a fresh shared store (`tests/shared_mode.py`). Two tests fail, both by
  design: `test_plain_memory_is_unchanged` asserts that a plain instance is not shared, and
  `test_save_isonl_suffix_is_still_supported` compares a plain `ISONGraph` count of the saved
  copy, which counts the `store_meta` node, with the shared graph's count, which excludes it.
  No defect was found. `python -m tests.eval.run --shared` prints a scorecard identical to the
  plain run (recall 1.00, leaks 0, abstention 1.00, held-out abstention 0.38).
- **`get_stats()`** took the lock once per store (15 times) and could mix states; it is now
  one transaction.
- **Performance** on the development laptop: contended write p95 3.1 ms (4 processes x 60
  writes; budget 15 ms), idle `refresh_if_stale` 0.014 ms (budget 0.2 ms), open of a
  5,000-record store 0.21 s (budget 1.5 s). Building that store is slow in both modes (about
  100 s for 5,000 `remember` calls), because each write scans the records of its kind; that
  is the writer's cost, not the storage's.
- Warnings recorded after `open()` (a skipped compaction, a skipped checkpoint at close,
  replay notes) go to the backend's `warnings` list; `load_warnings` is a copy taken at open,
  so they are not on a public attribute.
- The way back from a shared store to a plain file (12.5) was run in a temp directory. After
  open, write and close, the steps removed the journal and the lock, and a plain `load()` +
  `save()` kept every record. With a child that wrote and then exited through `os._exit`
  without closing, open + `checkpoint()` + close kept its record, and open + close alone did
  not. With the `after_append` fault point (record durable, call not returned) the record was
  kept; with `mid_append` (torn line, never acknowledged) it was dropped, as in §4.6.

### 12.5 Open decisions (not resolved here)

- Reads after `close()` raise for store methods (above). Whether reads should keep working
  on the last state, as `recall()` does, is open.
- `start_session` links a new session to the last session of any agent (§10).
- The CLI and the greenfield, governed and integration modules still use plain
  `load()`/`save()`. On a shared store the CLI's read commands work and its write commands
  raise `StoreInUse`. Sub-project 3 moves the CLI to `open()`.
- A path once opened with `open()` stays shared after every process has closed it: its
  `.journal` remains, and any plain `save()` to it (`load()` then `save()`, `AgentMemory(...)`
  then `save()`, every `amem` write command) raises `StoreInUse`. The refusal stays as
  designed: a plain save over a store that has a journal can write a snapshot whose recorded
  sequence number is stale relative to its content, so the next opener would re-apply journal
  ops. The way back to plain mode (verified, 12.4): with no other process using the store,
  `AgentMemory.open(path)`, `checkpoint()` (stop unless it returns `True`; the README's
  snippet raises instead of using `assert`, which `python -O` strips), `close()`, then delete
  `<path>.journal`. The explicit `checkpoint()` is required, because `close()` checkpoints
  only when that instance wrote: open-and-close alone left a crashed writer's acknowledged
  record outside the snapshot, and deleting the journal then lost it. `<path>.lock` is left
  in place: the plain-mode guard looks only at the journal, and deleting the lock file while
  a process still holds it would let a new process lock a fresh file beside it on POSIX.
- `close()` checkpoints only when this instance wrote (`_dirty`), so a store whose only
  content is a leftover journal is not folded by open and close. Whether `close()` should
  also checkpoint after replaying leftover ops is a product decision, left as is.
- After `close()`, `checkpoint()`, `refresh()` and `save()` to the store's own path still
  work, and `save()` to another path writes a copy of the state last seen (M1).

Known limits found by the final review and recorded for a follow-up (not fixed):

- A `KeyboardInterrupt` between `apply()` and `record()` in `SharedGraph._journaled` leaves
  a change in memory that never reached the journal; a later update of that node makes other
  processes raise `StoreCorrupt` on replay. Needs a mark-diverged-and-rebuild design (M3).
- No directory fsync on POSIX after `os.replace`, so a power loss right after a compaction
  can lose the rename (M4).
- The lock file is created with mode 0o777 (less the umask), and any `OSError` from the lock
  call is treated as "busy" until the timeout (M5).
- A shared `save()` to another path copies the origin's generation and its `store_meta`
  sequence into the copy (M6).
- `load()` of a shared store drops replay warnings: its `load_warnings` does not include them
  (M7).
- Test gaps: a lockless reader compared against the authoritative state under contention, and
  replay performance of a large journal (M8).
