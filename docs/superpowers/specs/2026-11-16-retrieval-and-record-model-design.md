# Retrieval and Record Model — Design Spec

Date: 2026-11-16
Status: draft, awaiting review
Scope: sub-project 1 of 3 (2 = concurrency-safe storage, 3 = MCP/CLI surface and agent instruction files)

## 1. Goal

Make `agent-memory` return the right memories for a coding task, and only those, with enough provenance that an agent can decide whether to trust each one.

"Best in the market" is treated here as a measurable target, not a claim. The product wins if, on a published evaluation set, an agent with this memory does better than the same agent without it, and memory never makes it worse. Section 9 defines that set and the pass thresholds.

### What the market does today

Based on a survey of public material (mostly vendor and secondary sources; none of the performance claims were verified):

| Approach | Examples | Weakness for coding work |
|---|---|---|
| Rule files | `CLAUDE.md`, `AGENTS.md`, Cursor rules | Always loaded, hand-maintained, no relevance ranking, no staleness |
| Chat-fact memory | Mem0, Letta, Supermemory | Remembers conversations, not code; facts are not tied to files or commits |
| Temporal knowledge graph | Zep / Graphiti | Strong invalidation model, but needs an LLM on write and a graph database |
| Code graph index | Cognee, codebase-memory-mcp, Serena | Indexes what the code *is*, not what the team *learned* |

Recent coding-memory benchmarks (SWE-Bench-CL, AMB, EvoArena) report the same failure: stale, superseded or merely adjacent memories mislead the agent, and several memory systems score no better than no memory.

### Where this design aims to be different

1. **Code-anchored.** A memory points at files and symbols, and knows the content hash it was written against. When the code changes, the memory is flagged.
2. **Abstains.** An irrelevant task gets an empty result, not the five most recent records.
3. **Never forgets silently, never lies silently.** Records are superseded or archived, not deleted; superseded records never reach the prompt.
4. **No LLM and no server at query time.** Retrieval is deterministic, local, and explainable per result.
5. **Typed for engineering.** Rejected alternatives, failed refactors and bug patterns are first-class guardrails, not generic "facts".
6. **Ships with its own evaluation.** The memory-on versus memory-off comparison is part of the repo.

## 2. Problems in the current code this spec fixes

Verified by reading the code and running a probe:

- Task text is not used for retrieval. Bugs and conventions match only on file-path substring; "decisions" returns the first five nodes.
- `CodingContext.to_prompt()` does not render decisions, module context or experts, so selected items are dropped.
- `check_suggestion` only fires on an exact substring of a rejected alternative.
- Token budget uses fixed per-type constants, and the packing loop stops at the first item that does not fit.
- Records carry no author, commit, evidence, confidence or status.
- IDs are built from truncated names (`conv_{category}_{rule[:20]}`); a second record with the same prefix raises `DuplicateNodeError`.
- Consolidation hard-deletes, and its merge step only counts duplicates.
- List properties come back from disk as JSON strings, and stores work around this with ad-hoc comma joins.
- The list of "which node types are memories, and which fields are their text" is duplicated in three modules.

## 3. Out of scope

- File locking, merge-safe storage layout, multi-process writes (spec 2).
- MCP server, CLI redesign, `AGENTS.md` / `CLAUDE.md` generation (spec 3).
- Automatic capture from transcripts or commits.
- LLM-based extraction, summarisation or contradiction detection.
- Removing the goals, todos, skills, team or governance modules. They keep working and are simply not retrievable kinds.
- Cross-project memory.

## 4. Record model

### 4.1 Envelope

Every retrievable node carries a common set of properties, the envelope, alongside its existing kind-specific properties. Existing node types and store methods stay.

| Field | Type | Meaning |
|---|---|---|
| `text` | str | Canonical one-statement form of the memory, derived from kind fields at read time (4.3). Not persisted. |
| `created_at`, `updated_at` | ISO-8601 UTC | Ingestion time. |
| `agent` | str | Who wrote it: `claude-code`, `codex`, `cursor`, `devin`, `human`, `bootstrap`, `unknown`. Free string. |
| `session_id` | str | Session that produced it, empty if none. |
| `source` | enum | `explicit` (agent or human said so), `extracted`, `bootstrap`, `imported`, `legacy`. |
| `evidence` | str | Why this is believed: commit SHA, PR, incident, `file:line`, or a sentence. |
| `commit` | str | Repo HEAD when written, empty if unknown. |
| `scope` | list of globs | Paths the memory applies to. `["*"]` means repo-wide. |
| `priority` | enum | `must`, `should`, `info`. |
| `confidence` | float 0–1 | Default 0.7 explicit, 0.5 extracted or bootstrap, 0.6 legacy. |
| `status` | enum | `active`, `superseded`, `retracted`, `archived`. |
| `invalid_at` | ISO-8601 UTC | When it stopped being true; empty while active. |
| `helpful`, `harmful` | int | Explicit feedback counts. |

Two relationships are edges, not properties, so the graph can traverse them:

- `ANCHORED_TO`: memory → `file` node (id = repo-relative POSIX path). Edge properties: `symbol` (optional), `hash` (content hash at write time), `commit`.
- `SUPERSEDES`: new memory → old memory. Already exists for decisions; generalised to all kinds.

Retrieval counters (how often a record was returned) are deliberately **not** stored on the record. Writing on every read would churn a file that is meant to be committed. The retriever emits a `RecallEvent` to a pluggable sink; the default sink is a no-op, and spec 2 decides where usage data lives.

### 4.2 Encoding

One codec, `record.py`, owns conversion between a `MemoryRecord` dataclass and node properties. Lists are JSON-encoded on write and decoded on read. Missing envelope fields on load are filled with `legacy` defaults, so existing `.memory` files load unchanged and are upgraded when next saved.

### 4.3 Kind registry

A single table replaces `TOKEN_ESTIMATES` and the `INDEXABLE_TYPES` table in `embeddings_integration.py`.

| Tier | Kinds | Default priority | Half-life |
|---|---|---|---|
| Guardrail | `alternative`, `bug_pattern`, `refactoring_attempt`, `quirk`, `hot_path`, `performance_constraint`, decision constraints (`CONSTRAINS` edges) | `should` | none |
| Guidance | `convention`, `idiom`, `project_pattern`, `decision`, `fix_pattern`, `workaround`, `test_convention`, `required_test_case`, `template`, `snippet` | `should` | 365 days |
| Background | `module`, `design_pattern`, `tech_debt`, `bug`, `session_decision`, `open_question`, `blocker`, `partial_work`, `expertise_area` | `info` | 30 days for session kinds, 180 otherwise |

`must` is never a default; it is set explicitly.

A `decision` is promoted to the guardrail tier for a query when it has a `CONSTRAINS` edge to a module containing one of the query files; the edge's `how` property is appended to its rendered text.

Each entry also declares the template that builds `text` from kind fields, for example `bug_pattern` → `"{name}: {signature}"`, `alternative` → `"Rejected: {option} — {rejected_because}"`.

Node types not in the registry (`agent`, `file`, `session`, `goal`, `task`, `skill`, `team_member` and so on) are structural and never retrieved.

### 4.4 Identity

New records get `"{kind}_{slug}_{h6}"`, where `slug` is the first 24 characters of the slugified text and `h6` is the first six hex characters of the SHA-1 of the normalised full text. Same text gives the same id; different text never collides on a shared prefix. Existing ids remain valid.

### 4.5 Write operations

Added to `AgentMemory`; existing store methods are rerouted through the same path via a `BaseStore._add_memory()` helper.

- `remember(kind, fields, *, anchors, scope, evidence, priority, confidence) -> WriteResult`
  - Same normalised text as an active record of the same kind: no new node. The existing record gains the new evidence and anchors, and confidence moves up by 0.1, capped at 0.95.
  - Lexically close to an active record of the same kind (token Jaccard ≥ 0.6): the record is written, and `WriteResult.similar` lists the neighbours so the caller can supersede. The library does not decide on its own.
- `supersede(old_id, new_record)`: writes the new record, adds `SUPERSEDES`, sets the old record's `status=superseded` and `invalid_at=now`.
- `retract(id, reason)`: `status=retracted`, `invalid_at=now`, reason appended to `evidence`.
- `feedback(id, helpful: bool)`: increments a counter.
- `anchor(id, path, symbol)`: adds an `ANCHORED_TO` edge to an existing record.
- `set_actor(agent, session_id)`: sets the author stamped on subsequent writes. `start_session` calls it.

`AgentMemory.__init__` gains optional `repo_root` and `agent` arguments. Both default to unset, which preserves current behaviour.

### 4.6 Consolidation

Reduced to what the record model needs:

- Decay sets `status=archived` instead of removing the node.
- Merge actually merges exact duplicates using the rule in 4.5.
- The "any edge means actively used" rule is removed; an `ANCHORED_TO` edge would otherwise make everything immortal.

## 5. Staleness

Each `ANCHORED_TO` edge is evaluated against the working tree when `repo_root` is set.

| State | Condition | Effect on retrieval |
|---|---|---|
| `fresh` | File hash equals the stored hash | None |
| `drifted` | File exists, hash differs, and the anchored symbol name (if any) still appears in the file | Score × 0.7; rendered with a "file changed since recorded" flag |
| `broken` | File exists but the anchored symbol no longer appears | Score × 0.4; flagged |
| `orphaned` | File is gone | Score × 0.2; excluded from `check()` |
| `unknown` | No `repo_root`, or no anchors | None |

A record with several anchors takes its worst state. Staleness is computed, never stored, so switching branches gives the right answer.

Hashing is SHA-1 over file content with line endings normalised to LF and trailing whitespace stripped, so a CRLF checkout on Windows does not mark everything drifted.

`verify(id)` re-stamps the anchors of a record at the current hash; an agent calls it after confirming a drifted memory still holds.

Symbol-level hashing through `code-flow-graph` is a later refinement. Version 1 is file-level hash plus symbol-name presence.

## 6. Retrieval

### 6.1 Interface

```python
result = memory.recall(
    task="add retry logic to the payment webhook",
    files=["webhooks/payment.py"],
    symbols=["handle_webhook"],      # optional
    kinds=None,                      # optional filter
    max_tokens=2000,
)
result.items        # list[ScoredMemory], ranked
result.to_prompt()  # markdown
result.to_dict()    # JSON-serialisable
```

`ScoredMemory` holds the record, final score, per-channel scores, staleness state, and a one-line `why` such as `"matched: retry, webhook · anchored to webhooks/payment.py"`.

`get_context()` keeps its signature and return type. It is reimplemented on top of `recall()`: `CodingContext` gains an `items` list and its category lists are filled from it.

### 6.2 Pipeline

All steps are deterministic and run in-process.

1. **Candidate set.** Registry kinds with `status=active`.
2. **Channels.** Each produces a ranked list.
   - *Lexical.* BM25 over `text`. The tokeniser splits on non-alphanumerics, `snake_case`, `camelCase` and path separators, lowercases, drops a small stopword list, and applies light suffix stripping (`-s`, `-es`, `-ies`, `-ing`, `-ed`). The query is task text plus symbol names plus file basename stems.
   - *Scope.* Records whose `scope` glob matches a query file or which are `ANCHORED_TO` one. Score is 1.0 for the same file, 0.6 for the same directory, 0.3 for a shared ancestor two or more levels deep. `["*"]` does not match in this channel.
   - *Graph.* One hop from the top lexical and scope hits along a whitelist: `FIX_FOR`, `WORKAROUND_FOR`, `HAS_QUIRK`, `CONSIDERED_FOR`, `CONSTRAINS`, `ATTEMPTED_ON`, `INSTANCE_OF`, `TEST_FOR_BUG`. A neighbour inherits half the seed's fused score directly; it does not enter rank fusion. This is how a bug pattern brings its fix along.
   - *Semantic.* Optional. Used when `ison-graph-embeddings` is installed and an index exists. Absent otherwise; nothing else changes.
3. **Fusion.** Weighted reciprocal rank fusion. Initial weights: lexical 1.0, scope 1.0, semantic 1.0. These are constants in one place, tuned against the evaluation set.
4. **Modifiers.** Multiply the fused score by: `confidence`; the staleness factor (section 5); recency `0.5 ** (age / half_life)` floored at 0.3; tier boost (guardrail 1.3, guidance 1.0, background 0.8); feedback `(1 + helpful) / (1 + helpful + 2 × harmful)`.
5. **Relevance floor.** A record survives only if it has a lexical hit on at least one non-stopword query term, or a scope score ≥ 0.6, or a semantic score above threshold, or it arrived by graph hop from a survivor. Everything else is dropped. An empty result is a valid result.
6. **Pinned set.** Records with `priority=must` and `scope=["*"]` bypass the floor, capped at 15% of the budget and ordered by confidence. This is the only always-on path, and it is small by construction.
7. **De-duplication.** Records with token Jaccard ≥ 0.8 collapse to the highest-scored one.
8. **Budget packing.** Greedy by score. Token cost is measured on the rendered line by a pluggable `TokenCounter` (`ceil(chars / 4)` by default; `tiktoken` is opt-in because loading an encoding can require a download). An item that does not fit is skipped and packing continues.
9. **Session continuity.** Open questions, blockers and partial work from the most recent unfinished session go through the same pipeline as background kinds; they are no longer appended outside the budget.

### 6.3 Rendering

One generic renderer driven by `text`, grouped by tier. No per-kind code, so no kind can be left out.

```
## Guardrails
- [bug_pattern_webhook_storm_3fa2c1] webhook_storm: unbounded retry on external webhook
  why: matched retry, webhook · anchored to webhooks/payment.py · claude-code, 2026-10-02
  fix: cap attempts; see fix_pattern_cap_retries_91be07
## Guidance
- [decision_cap_payment_webhook_r_77d0aa] Cap payment webhook retries at 5 — unbounded retry caused INC-2026-03
  ⚠ webhooks/payment.py changed since this was recorded
```

Ids are included so the agent can call `feedback`, `verify` or `supersede`. A `compact=True` mode drops the `why` lines.

### 6.4 Guardrail check

```python
warnings = memory.check(approach="Retry indefinitely with exponential backoff",
                        files=["webhooks/payment.py"])
```

`check()` is `recall()` restricted to guardrail kinds, with the approach text as the query, a stricter floor (two matching terms, or one term plus a scope hit, or one term that appears in at most two guardrail records), and orphaned records excluded. It returns the same `ScoredMemory` objects.

It reports *relevant prior negative experience*. It does not claim to detect contradiction; that needs a model and belongs to the calling agent. `check_suggestion()` stays as a thin wrapper that returns the old dict shape.

### 6.5 Index lifetime

There is no persistent index. `recall()` tokenises on demand with a per-process cache keyed by `text`, so unchanged records cost a dictionary lookup. Target: p95 under 50 ms at 5,000 records, lexical and scope channels, on a laptop. If measurement misses that, the fallback is a revision counter bumped by `_add_memory`. As built, a snapshot-validated record cache is used instead; see section 12.

## 7. Module layout

| File | Responsibility | Depends on |
|---|---|---|
| `record.py` | `MemoryRecord`, envelope codec, kind registry, id generation | `ison_graph` |
| `anchors.py` | Content hashing, anchor edges, staleness evaluation | `record.py` |
| `retrieval/tokenize.py` | Tokeniser and stemmer | — |
| `retrieval/lexical.py` | BM25 | `tokenize.py` |
| `retrieval/channels.py` | Scope, graph and semantic channels | `record.py`, `anchors.py` |
| `retrieval/retriever.py` | Fusion, modifiers, floor, de-duplication | the above |
| `retrieval/budget.py` | `TokenCounter`, packing | — |
| `retrieval/render.py` | Markdown and dict rendering | `record.py` |
| `context.py` | `CodingContext` as a view over `RecallResult` | `retrieval/` |
| `memory.py` | `recall`, `check`, `remember`, `supersede`, `retract`, `feedback`, `verify`, `set_actor` | all |
| `stores/base.py` | `_add_memory()` helper | `record.py` |

Stores that create registry kinds switch their `graph.add_node` calls to `_add_memory()`. Their public signatures do not change. `embeddings_integration.py` reads its field lists from the registry.

## 8. Compatibility

- All 79 existing tests pass without edits.
- Existing `.memory` files load; missing envelope fields get `legacy` defaults.
- `get_context`, `get_quick_context`, `check_suggestion` keep their signatures and return shapes.
- One intended behaviour change: `get_context` no longer returns every idiom, every project pattern and five arbitrary decisions regardless of task. If an existing test depends on that, it is updated and called out in the plan.
- `DESCRIBE.md` and `README.md` are corrected to the real API as part of this work.
- No new required dependencies.
- The `ison-graph` floor moves from `>=1.2.0` to `>=1.4.1`, the current release. The design needs no new ISONGraph feature, but 1.4.1 fixes four cases where a graph changed meaning between save and load, and the record model depends on that round trip being exact. The development machine currently has 1.4.0 installed and is upgraded as the first implementation step.

## 9. Evaluation

A scenario suite under `tests/eval/`, run by pytest and by a script that prints a scorecard. Each scenario is a small memory fixture, an optional file tree, a query, and expected `must_include` / `must_exclude` ids.

Conditions, at least six scenarios each, 48 or more in total:

| Condition | Expectation |
|---|---|
| Relevant, task text only | Target record in result |
| Relevant, files only | Target record in result |
| Absent (nothing relevant stored) | Empty result apart from the pinned set |
| Irrelevant (unrelated memories only) | Empty result apart from the pinned set |
| Adjacent (same vocabulary, different module) | Target ranked above the adjacent record |
| Superseded | New record present, old record absent |
| Stale (anchored file changed or deleted) | Record flagged, ranked below a fresh equivalent |
| Guardrail (`check`) | Rejected alternative or bug pattern returned for a paraphrased approach |

Pass thresholds for this spec:

- `must_include` recall ≥ 0.90 at a 2,000-token budget.
- Superseded or retracted records in output: 0.
- Abstention on absent and irrelevant scenarios ≥ 0.90.
- Every selected item appears in the rendered prompt (property test).
- Measured token count of the rendered prompt ≤ `max_tokens` (property test).
- p95 latency under 50 ms at 5,000 records.

The current implementation is run through the same suite first, to record a baseline in the repo.

End-to-end comparison against other products, with a real agent solving tasks memory-on and memory-off, needs the MCP surface from spec 3 and is planned there. This suite is the retrieval-level gate.

## 10. Error handling

- No `repo_root`: staleness is `unknown`, scope and anchors still work on the paths given.
- Anchor path outside the repo or unreadable: anchor recorded without a hash, state `unknown`.
- Unknown `kind` passed to `remember`: `ValueError` listing valid kinds.
- Malformed envelope value on load (bad JSON list, non-numeric confidence): field falls back to its default and the record id is collected into `memory.load_warnings`; loading does not fail.
- Semantic channel raises: logged once, channel skipped for that call.

## 11. Open risks

- **Lexical-only recall on paraphrase.** "Retry forever" versus "unbounded retry" share one stem. The guardrail scenarios will show how far BM25 plus stemming gets; the semantic channel is the planned answer if it falls short, and that would make embeddings a recommended rather than optional install.
- **File-level drift is noisy.** A busy file marks every memory anchored to it as drifted. The 0.7 factor is mild for this reason; symbol-level hashing is the follow-up.
- **Fusion weights are guesses** until the evaluation set exists. They are isolated in one place for that reason.
- **An MCP server already exists** at `../isongraph/tools/mcp-servers/agent-memory-mcp`, exposing 37 tools that map one-to-one onto store methods. It is not touched here. Spec 3 decides whether to reduce it to the handful of verbs defined in this spec (`recall`, `check`, `remember`, `supersede`, `feedback`, `verify`) or replace it.
- **Two copies of this package exist** (`../isongraph/tools/agent-memory` is the one pip currently has installed, and 13 files differ). This spec targets the copy in this directory. Which one is canonical needs deciding before implementation.

## 12. Implementation notes

What changed during implementation that the sections above do not state:

- **Record cache.** The retriever uses a snapshot-validated record cache, not the writer revision counter mentioned in 6.5. Stores call `graph.update_node` directly, so a revision counter bumped by the writer would serve stale records.
- **Lexical indexes.** BM25 indexes are kept per candidate-set role (an LRU of 4) and updated incrementally in place.
- **Detached copies.** Records returned from `recall()` and `check()` are detached copies; mutating them does not touch the graph.
- **Tokeniser.** `compound_tokens` adds the camelCase-joined form on both the document and the query side. Stemmer rules: un-doubling is restored (`running` -> `run`), and a plural `-ings` keeps `-ing` when the stem is three letters or fewer.
- **Decay.** The consolidator's decay reads `created_at` as aware UTC and only touches active records. Retrieval's recency modifier is separate and uses `updated_at or created_at`. Decay never archives a guardrail-tier kind (their half-life is none, 4.3) or a record with `priority="must"`. The final review also asked decay to age a record from `updated_at` (so a reinforced record is not old); that was not done, because an existing test, `test_writer_record_decays_via_created_at`, asserts decay from `created_at` while `updated_at` is fresh, and changing it was refused in the fix wave. The two cannot both hold; deciding which is right is open.
- **Latency gate.** The strict 50 ms gate is opt-in (`pytest -m perf`). A loose always-on guard stays in the default suite, because wall-clock gates are flaky on a throttled machine.
- **Store ids.** `BaseStore._make_id` keeps its space, `/` and `.` replacements and lowercasing, and additionally replaces space, tab, CR, LF, `:` and `|` (exactly the characters ISONGraph rejects in node ids) with `_`. Ids of already-valid names, including non-ASCII ones, are unchanged.
- **Encoding (4.2) deviation.** Legacy records are not "upgraded when next saved". Missing envelope fields are not written back; the defaults are reapplied by `MemoryRecord.from_node` on every read, so a legacy file saved again stays in its legacy form, and only records written or updated through the writer carry the full envelope.
- **Anchors outside the repo (section 10).** A path outside `repo_root` is recorded without a hash, so its state is `unknown` and the file is never read. An absolute path is never persisted: an absolute path outside the repo is stored as `outside/<file name>`; a relative `../` path is stored as given. "Outside" is judged lexically (`a/../../x.py` is outside, `lib/../src/a.py` is inside), so a symlink inside the repo that points out of it is still read. The location field never auto-anchors a file outside the repo. Files over 2 MB (`anchors.MAX_HASH_BYTES`) are never read or hashed: an anchor to one has no hash, and a file that grows past the cap reads as `unknown`, not `orphaned`.
- **Held-out abstention and the recall floor.** The main suite's abstention (1.00) was tuned in-sample. A held-out set of 16 unrelated tasks (`tests/eval/heldout.py`, printed by `python -m tests.eval.run`) scores 0.38: the floor lets a record through on any single shared non-stopword stem ("writ", "document", "set"). A stricter "informative match" floor (two concepts, a scope hit >= 0.6, a semantic hit, a df-distinctive term, or a graph hop) was tried and reverted: it left held-out at 0.38, because in a small corpus nearly every term has df <= 2, and it dropped main recall to 0.97. Without the distinctive-term clause held-out reached 1.00 but main recall fell to 0.90. A lexical floor cannot separate a spurious single-stem match from a real single-concept one; the semantic channel is the planned answer. Numbers are in `tests/eval/RESULTS.md`.
- **Session carry (6.2).** Open items (open_question, blocker, partial_work) are carried only from the writer's current session unless it has ended, else from the most recently started unended session if it started within 14 days (the open_question decay window), at most 10 items, newest first. A crashed agent's session therefore stops being carried after 14 days. An answered question or resolved blocker is never a merge target, so re-asking it is a new open item; re-recording an open item in a new session moves it to that session.
- **Filtered `get_context`.** Pinned (must/"*") and carried items are kept whatever `exclude` says, and dropped only when `include` is given and names none of their buckets. `bug_risks` entries keep the shape `{"type", "pattern", "fix"}` (the fix is attached through `FIX_FOR`, or `None`), and `session_context` has all six keys of `SessionStore.get_session_context` whenever a session exists.
- **Natural keys and name lookups.** A store "add" of a module, decision, hot path, performance constraint, expertise area, design pattern or tech debt whose key already has an active record with different text supersedes that record; the new one takes over its edges (anchors are re-stamped). Store methods that rebuild an id from a name or text prefix resolve it through `MemoryWriter.resolve`: the record whose name or text matches exactly, active before inactive, open before answered or resolved; `ValueError` when several match equally.
- **Constraint text.** For a decision promoted by a `CONSTRAINS` edge, the edge's `how` text is part of the record's lexical document for that query (scored with the corpus statistics) and of the `check()` strict floor.
