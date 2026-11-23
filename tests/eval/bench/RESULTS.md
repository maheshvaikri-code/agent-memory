# Benchmark results

Baseline: the unchanged retriever (branch feature/recall-quality, no retrieval code changed), run with
`PYTHONPATH=src python -m tests.eval.bench.run --split dev|test --label baseline --save`.
Saved runs: `tests/eval/bench/runs/dev-baseline.json`, `tests/eval/bench/runs/test-baseline.json`.
The test split was run twice in total: once as this baseline, and once on the final retriever (see "Final
result: test split" below, `tests/eval/bench/runs/test-after.json`). It is not run again.
Read "Final review findings (retrieval levers)" at the end before relying on the lever results: the whole-branch
review recommends against merging them as configured.

## Baseline, dev split

```
split dev, label baseline
tasks 111 (empty 37)
must-include recall   0.9875  (158/160)  95% CI 0.956-0.997
clean-abstain rate    0.0000  (0/37)  95% CI 0.000-0.094
noise per task        18.441 records, 1139.4 tokens
superseded/retracted leaks  0
```

## Baseline, test split (locked)

```
split test, label baseline
tasks 215 (empty 70)
must-include recall   0.9723  (246/253)  95% CI 0.944-0.987
clean-abstain rate    0.0000  (0/70)  95% CI 0.000-0.052
noise per task        18.865 records, 1166.1 tokens
superseded/retracted leaks  0
```

## In-sample scorecard (`python -m tests.eval.run --service`)

50 scenarios, all conditions pass (task_only 6/6, files_only 6/6, absent 6/6, irrelevant 6/6,
adjacent 6/6, superseded 6/6, stale 6/6, guardrail 8/8); must-include recall 1.00 (30/30);
superseded or retracted leaks 0; abstention 1.00 (12/12).

## Held-out abstention

| Figure | Result |
|---|---|
| Original (headline) | 0.38 (6/16) |
| Relabelled after review, not substituted | 0.50 (8/16) |

The relabelled figure marks one returned record as acceptable for each of three tasks
("refactor the logging setup for the CLI": structlog; "improve error messages in the onboarding
wizard": apierror; "configure the sentry error reporting DSN": apierror). Relabelling after
seeing results is a form of tuning, so the original 0.38 stays the headline.

## Latency, 5,000 records (median recall, unchanged retriever)

Measured after a rest, twice: 36.3 ms, then 35.5 ms (consistent, no sign of throttling).

## Label corrections / world changes

No label was corrected after the test split was locked. One label edit was made on the dev split before any
retrieval run, in the Task 8 fix, and was not listed here until now: commit 4e61d6e demoted `bug-migrate-crash`
from `relevant` to `acceptable` in `dev-mobile.json` (the elevation-column task), so that the task stays at
four relevant records or fewer once `td-json-columns` was promoted from `acceptable` to `relevant`. Dev
split only; no test label or world changed.

`tests/eval/bench/bench.sha256` now locks the three worlds, `model.py`, `judge.py`, `build.py` and `stats.py`
(`test_benchmark_definition_files_are_unchanged`); a later change to any of them is recorded in this section.

## Known borderline labels

Found in review of the locked test split. Not corrected in place; the locked labels stand.

### test-saas

1. stale-cache-shape / cache-version-bump are acceptable for the board-list-comments task though dev labels them relevant for the serializer task; openapi-drift is acceptable but openapi-regenerate is unlabelled.
2. "migrate CI from CircleCI to GitHub Actions" is empty though openapi-regenerate is arguably acceptable.
3. "custom 500 error page" is empty though api-error-shape is arguably acceptable.
4. plans-module is relevant here versus acceptable in dev.
5. job-idempotent is only acceptable on the sample-project job task.
6. The "move to top" task lacks position-collision / position-rebalance-fix.
7. wizard-refresh-bug relevant is borderline.
8. softdelete-manager-quirk is unlabelled on the XLSX task.
9. import-checkpoint is acceptable versus relevant in dev.
10. cursor-pagination is unlabelled on the trash view task.
11. Three tasks share a template with dev tasks.
12. cache-key-builder / cache-shared-plan are unlabelled on the subscription.deleted task.

### test-pipeline

- announce-backfills is relevant in the catchup task but acceptable in the 30-day chunk task.
- orders-backfill-progress is relevant versus announce-backfills acceptable on the file-only backfill.py task.
- sunday-drop-late is acceptable on the London-time task (arguably relevant).
- The Jenkins-to-GitHub-Actions CI-runner task is empty with dagbag-* acceptable (arguably relevant).
- The pre-commit task conflicts with an existing hook.
- metrics-latency is acceptable on the raw-rows endpoint task versus relevant in dev.
- Test tasks reuse dev templates and collision shapes.
- Two tasks copy record wording.
- 8 (not 7) empty tasks carry acceptable records.

### test-mobile

- quirk-play-version is unlabelled on the Play-hold task.
- perf-sync-batch is unlabelled on the API-timeout task.
- tc-sync-fake-timers is acceptable on the sync-test-split task (dev: relevant).
- Two tasks are close to dev templates.
- Two tasks echo record wording.
- The db.ts file-only task labels bug-migrate-crash relevant while schema tasks label it acceptable.

## Lever trials (dev split)

| Trial | Recall | Noise tokens per task | Clean-abstain | Leaks |
|---|---|---|---|---|
| baseline | 0.9875 (158/160) | 1139.4 | 0.0000 (0/37) | 0 |
| lever 1: drop weak-only, RARE_SHARE=0.15 (SMALL_STORE=25, whole-identifier rule, pinned exempt) | 0.9875 (158/160) | 1078.6 | 0.0000 (0/37) | 0 |
| lever 5: small-store rule, SMALL_STORE=25 (set in the lever 1 commit; pinned by boundary tests here) | 0.9875 (158/160) | 1078.6 | 0.0000 (0/37) | 0 |
| lever 2: graph seeds from strong and medium survivors only (no effect on the benchmark; kept because weak survivors exist in small stores and for weakly matched pinned records) | 0.9875 (158/160) | 1078.6 | 0.0000 (0/37) | 0 |
| lever 3: generic task-word list (pre-registered list minus `rename`; a generic word counts toward a record with a second concept, spec reading) | 0.9875 (158/160) | 1013.6 | 0.0000 (0/37) | 0 |
| lever 7: absolute cap on the rare tier, RARE_CAP=10 (added after seeing the dev results of levers 1-3; not a pre-registered lever) | 0.9875 (158/160) | 947.8 | 0.0000 (0/37) | 0 |

### Lever 7: absolute cap on the rare tier (dev split)

Lever 7 was added by the controller (ruling R14) after seeing the dev results of levers 1-3. It is not one
of the spec's pre-registered levers, and it is judged on the locked test split like the rest. A word is
"rare" if it is in at most max(DISTINCTIVE_DF, RARE_SHARE of the records) records, and never in more than
`RARE_CAP` records however large the store (`RARE_CAP = None` means no cap; when the cap is below
DISTINCTIVE_DF the cap wins). Each cap was set for the run only (not saved); "In-sample" is
`python -m tests.eval.run --service` with the same cap, "Held-out" its held-out abstention figure.

| RARE_CAP | Dev recall | Noise records per task | Noise tokens per task | Clean-abstain | Leaks | In-sample recall / leaks / abstention | Held-out | Eligible |
|---|---|---|---|---|---|---|---|---|
| None | 0.9875 (158/160) | 16.27 | 1013.6 | 0/37 | 0 | 30/30, 0, 12/12 | 12/16 | yes |
| **10** | **0.9875 (158/160)** | **15.21** | **947.8** | **0/37** | **0** | **30/30, 0, 12/12** | **12/16** | **yes (chosen)** |
| 8 | 0.9812 (157/160) | 13.29 | 832.0 | 0/37 | 0 | 30/30, 0, 12/12 | 12/16 | no (dev recall) |
| 6 | 0.9812 (157/160) | 12.56 | 790.5 | 0/37 | 0 | 30/30, 0, 12/12 | 12/16 | no (dev recall) |
| 5 | 0.9812 (157/160) | 11.49 | 727.8 | 0/37 | 0 | 30/30, 0, 12/12 | 12/16 | no (dev recall) |
| 4 | 0.9812 (157/160) | 10.62 | 674.5 | 1/37 | 0 | 30/30, 0, 12/12 | 12/16 | no (dev recall) |
| 3 | 0.9750 (156/160) | 9.64 | 612.4 | 1/37 | 0 | 29/30, 0, 12/12 | 12/16 | no (dev recall, in-sample) |
| 2 | 0.9688 (155/160) | 8.73 | 556.5 | 1/37 | 0 | 29/30, 0, 12/12 | 12/16 | no (dev recall, in-sample) |

Eligibility (same spirit as R2): in-sample recall 30/30, leaks 0, abstention 12/12, and dev recall at least the
baseline's 158/160. Eligible: None and 10. Chosen: 10, the smaller noise (947.8 against 1013.6 tokens per
task; 17% below the baseline's 1139.4; 6.5% below lever 3). The clean-abstain rate stays 0/37.

Dev relevant records lost (the baseline and lever 3 already miss `backfill-billing-night` and `sunday-drop-late`):

| RARE_CAP | Additional dev relevant records lost |
|---|---|
| 10 | none |
| 8, 6, 5, 4 | `fx-weekend-gap` ("resume the fact_orders backfill that stopped in March") |
| 3 | the above and `declare-grain` ("write a new incremental transform that rolls orders up into a weekly customer summary", also the in-sample loss) |
| 2 | the above and `hmac-tokens` ("a customer emailed asking us to erase everything we hold about them, handle the ticket") |

The held-out figure does not move for any cap (12/16). Saved run: `tests/eval/bench/runs/dev-lever7.json`.

### Lever 3: the generic task-word list (dev split)

Dev trial, saved run `tests/eval/bench/runs/dev-lever3.json` (`--label lever3`). Noise falls from 1078.6
to 1013.6 tokens per task (6%; 11% against the baseline's 1139.4). Clean-abstain stays 0/37: the
remaining noise on empty tasks is mostly ordinary topic words (`call`, `file`, `screen`, `customer`,
`test`) that are not task-opening words. In-sample scorecard unchanged: recall 30/30, leaks 0, abstention 12/12.

Counting variants (R9). The spec says a generic word still counts toward a record that has a second matched
concept; the plan's code counts only informative concepts. Both were measured on dev with the final list:

| Variant | Recall | Noise tokens per task | Clean-abstain | Leaks |
|---|---|---|---|---|
| spec reading: `concepts` = all distinct matched concepts, provided one is informative; only-generic means weak | 0.9875 (158/160) | 1013.6 | 0/37 | 0 |
| plan's stricter reading: `concepts` = informative concepts only | 0.9875 (158/160) | 1009.1 | 0/37 | 0 |

The difference is 4.5 tokens per task (0.4% of noise), below the 3% difference in noise tokens per task set as the bar for treating the two readings as different,
so the spec reading ships. The same change makes the whole-identifier rule test the informative terms: a generic word
that is also a record's own compound token (a caller symbol `setup` against a record that writes `setUp`)
no longer lifts that record by itself.

List edits (dev split only; every edit is mirrored in `test_the_generic_word_list_is_exactly_the_registered_one`):

- Removed `rename`. With the full pre-registered list, dev recall fell to 0.9812 (157/160): the task
  "rename tasks.status to tasks.state" (saas) lost its relevant record `expand-then-contract`, which it
  matched only on `renam`. Without `rename`: recall 0.9875 (158/160), noise 1013.6 (with it, 1012.4).
- Added nothing. Candidate additions were measured one at a time on dev (baseline for this comparison:
  1013.6 tokens per task, empty-task noise 792 tokens; recall stayed 158/160 and leaks 0 for each):

  | Candidate | Dev empty tasks it matched in | Noise tokens per task | Clean-abstain |
  |---|---|---|---|
  | `test` | saas, pipeline, mobile | 987.0 | 0/37 |
  | `call` | saas, pipeline | 1000.6 | 1/37 |
  | `up` | saas, pipeline, mobile | 1000.9 | 0/37 |
  | `file` | saas, pipeline | 1008.7 | 0/37 |
  | `name` | saas, mobile | 1007.3 | 0/37 |

  None was added. `test` is a topic with its own records (test conventions are relevant to "add a unit
  test"), not a task-opening word; `up` is a particle, not a coding verb or noun; `call`, `file` and `name`
  are topic words that match by coincidence ("on-call", "calls /healthz") and gain at most 1.3% each. A
  short list that rests on the pre-registered words beats one fitted to the dev noise.

Saved run: `tests/eval/bench/runs/dev-lever3.json`.

### Lever 1: RARE_SHARE grid (dev split, SMALL_STORE=25, with the identifier rule)

Each share was set for the run only (not saved). "In-sample" is `python -m tests.eval.run --service`
with the same share; acceptance rule 6 needs recall 30/30, leaks 0, abstention 12/12.

| RARE_SHARE | Dev recall | Noise tokens per task | Clean-abstain | Leaks | In-sample recall / leaks / abstention |
|---|---|---|---|---|---|
| 0.02 | 0.9688 (155/160) | 565.3 | 0.0270 (1/37) | 0 | 29/30, 0, 12/12 (not eligible) |
| 0.04 | 0.9750 (156/160) | 630.8 | 0.0270 (1/37) | 0 | 29/30, 0, 12/12 (not eligible) |
| 0.06 | 0.9812 (157/160) | 763.7 | 0.0000 (0/37) | 0 | 29/30, 0, 12/12 (not eligible) |
| 0.10 | 0.9812 (157/160) | 894.0 | 0.0000 (0/37) | 0 | 29/30, 0, 12/12 (not eligible) |
| **0.15** | **0.9875 (158/160)** | **1078.6** | **0.0000 (0/37)** | **0** | **30/30, 0, 12/12 (eligible)** |

Chosen: 0.15, by ruling R2. Shares 0.02 to 0.10 each lose the in-sample task_only scenario
"write a database migration for the orders table" (29/30), so they are not eligible. Of the
eligible shares (only 0.15), dev recall equals the baseline's (158/160). Lever 1 alone cuts dev noise
by only 5% (1139.4 to 1078.6 tokens per task) and does not abstain on empty tasks; the later levers carry
the reduction. The identifier rule lifts a weak match to medium in two cases only: (1) the task writes a camelCase
identifier (for example "GitHub", "StripeSignatureError") and the record matched its joined form;
(2) a term given as a symbol (the service passes the identifiers of a context trace as one lower-cased
symbol) matches the record's own compound token, so "github" given as a symbol lifts a record that
writes "GitHub" but not one that writes "github". A plain lower-case task word never qualifies by how a
record spells it. Dev at RARE_SHARE=0.15 with and without the rule: both recall 0.9875 (158/160), noise
1078.6 tokens per task, clean-abstain 0/37, leaks 0, so the dev split does not exercise the rule (it
exists for the in-sample `test_context_keeps_the_token_budget` case). The 0.02 to 0.06 noise figures above
were measured with the rule on, as were the in-sample columns. Pinned (must, scope "*") records bypass the
weak floor.
Saved run: `tests/eval/bench/runs/dev-lever1.json`.

## Decision: drop vs tail

Written before the single test-split run of the final retriever.

The rule, fixed in advance: adopt the tail (lever 4: weak matches go to a capped "possibly related" section
instead of being dropped) if and only if dev drop-recall is below the baseline's AND the tail brings it within
0.5 point of the baseline while keeping the noise-token cut at 50% or more.

The dev figures of the final configuration (`runs/dev-lever7.json`): drop-recall 0.9875 (158/160), equal to the
baseline's 0.9875 (158/160); noise 947.8 tokens per task (15.21 records per task) against the baseline's
1139.4 tokens (18.441 records); clean-abstain 0/37; leaks 0. In-sample scorecard 30/30 recall, 0 leaks, 12/12
abstention. Held-out abstention 12/16 (the baseline is 6/16 on the original labels, 8/16 relabelled).

Outcome: the first condition (drop-recall below the baseline's) is not met, so the rule is decided by it:
keep `drop`. The tail arm was not built, so its own cost in noise tokens (tail items count as shown) was
not measured; there is nothing to remove from `src/`.

Levers that shipped: lever 1 (drop weak-only matches; RARE_SHARE 0.15, pinned records exempt, identifier
lift), lever 5 (small-store rule, SMALL_STORE 25), lever 2 (graph seeds from non-weak survivors), lever 3 (the
generic task-word list, the pre-registered list minus `rename`) and lever 7 (RARE_CAP 10). Levers that did
not: lever 6 (rank-relative score cut) was ruled out in advance and measured, not shipped; lever 4 (the tail)
was not built. Lever 7 was added after the dev results of levers 1-3 were seen; it is not a pre-registered
lever.

## Not measured by the benchmark

- Small-store rule (lever 5). Every benchmark world holds at least 75 records, so `SMALL_STORE` (25)
  cannot be tuned on it: the dev split is identical to lever 1 (recall 0.9875 (158/160), 1078.6 noise
  tokens per task, clean-abstain 0/37, leaks 0) and the in-sample scorecard is unchanged (30/30, 0 leaks,
  12/12 abstention). The value is set by reasoning (a store of fewer than 25 notes costs little to
  over-show, and a real match is worth more) and pinned by the boundary tests in
  `tests/test_evidence_floor.py`: 24 records keep weak matches, 25 do not.
- `bm25.n`, the count the rule compares with `SMALL_STORE`, is the candidate count of the role being
  ranked, not the number of active records in the store. `recall(kinds=[...])` on a large store that holds
  fewer than 25 records of the requested kinds therefore keeps weak matches. This is the present
  behaviour, pinned by `test_small_store_is_counted_per_role_not_over_the_whole_store`, so a change to it
  is deliberate.
- The tail's own cost (lever 4). The tail was not built, so what it would have shown as noise, and whether it
  would have recovered the three relevant records lost on the test split, is not measured.
- Store sizes above about 100 records for `RARE_CAP` (10). Every benchmark world holds 75 to 100 records;
  the cap's effect on a store of several hundred or thousands of records is not measured (the 5,000-record
  latency probe holds synthetic quirks, not labelled tasks).
- Real user stores. The worlds are written, not collected.

## Lever 6: rank-relative score cut (dev split, trial only, not shipped)

Ruled out in advance and measured for the record. After the relevance floor, a non-pinned item scoring
below a fraction `f` of the best non-pinned score is dropped (pinned records are never cut). Applied by
`tests/eval/bench/arms.py` to the final retriever (lever 7 included) for the run only; `src/` is unchanged.
Dev split, 111 tasks (37 empty). The unpatched run in the same script reproduces the final retriever:
158/160, 947.8 tokens, 0/37, 0 leaks.

| Setting | Dev recall | Noise records/task | Noise tokens/task | on empty tasks | on non-empty tasks | Clean-abstain | Leaks |
|---|---|---|---|---|---|---|---|
| final retriever (no cut) | 0.9875 (158/160) | 15.21 | 947.8 | 741.1 | 1051.2 | 0/37 | 0 |
| f = 0.25 | 0.9875 (158/160) | 14.72 | 918.4 | 721.5 | 1016.8 | 0/37 | 0 |
| f = 0.5 | 0.8500 (136/160) | 7.19 | 447.9 | 494.9 | 424.4 | 0/37 | 0 |
| f = 0.75 | 0.6312 (101/160) | 2.77 | 172.2 | 228.4 | 144.0 | 1/37 | 0 |

Reading: the cut loses `relevant` records before it removes noise, as expected. At f = 0.25 it loses none
and removes 3% of the noise tokens (918.4 against 947.8). At f = 0.5 it halves the noise (53% below the
final retriever) but loses 22 of the 158 hits, and clean-abstain stays 0/37: the noise on an empty task is
not a low-scored tail, it is the whole result, so a cut relative to the best score cannot empty it. Relevant
records lost beyond the two the final retriever already misses: f = 0.5 loses 21 keys, mostly `fix-*` records
(`compact-after-load`, `cursor-paging-fix`, `expand-then-contract`, `fix-backoff`, `fix-fgs`, `fix-gps-spike`,
`fix-gpx-naive-time`, `fix-gpx-xxe`, `fix-idempotency`, `fix-migrate-crash`, `fix-pending-purchase`,
`fix-sourcemaps`, `fx-weekend-gap`, `gold-read-model`, `job-idempotent`, `lazy-dag-config`,
`micros-timestamp-fix`, `replace-partition-load`, `salt-the-key`, `severity-levels`, `slack-429-requeue`);
f = 0.75 loses these and about thirty more (all listed in `runs/dev-semantic-arms.json`). That the lost records are
fix records is consistent with them reaching the result through graph links at an inherited, lower score
(not checked record by record).

## Semantic arm (dev split, measurement only)

Nothing here ships. The default product stays zero-dependency; this section measures what an embedding
channel would do and cost. Dev only: the locked test split was not touched.

- Model: `BAAI/bge-small-en-v1.5` (384 dimensions), local Hugging Face cache snapshot
  `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`, loaded offline (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`),
  CPU only. sentence-transformers 5.1.2, torch 2.5.1. The `ison_graph_embeddings` package is not importable
  here, so `GraphEmbeddedMemory` could not be used; the adapter below stands in for it.
- Adapter (`EmbeddingAdapter` in `tests/eval/bench/arms.py`): at attach time it embeds the text of every active
  record of the world once (`MemoryRecord.text`, the rendered text, as `GraphEmbeddedMemory.index_all` does),
  normalised, in one batch. `search(query, k)` encodes the query (normalised, no instruction prefix unless
  stated), takes cosine against all record vectors and returns the top k as
  `agent_memory.embeddings_integration.EmbeddedResult` objects, the shape `GraphEmbeddedMemory.search` returns
  and `Retriever._semantic_scores` reads (`.node_ref`, `.score`). It is attached with
  `AgentMemory.attach_semantic`, the product's own hook, and recall goes through `MemoryService` as in the
  benchmark. It does not index records written after attach (a real channel would need incremental indexing).
- Arm A is the existing additive channel as designed: a record in the top `SEMANTIC_K` (20) hits with cosine
  >= `SEMANTIC_FLOOR` is strong evidence (`classify(semantic=True)` returns strong) and also a third RRF channel.
- Arm B is an exploratory confirmation filter, not an existing design. The retriever is unchanged except that a
  record whose lexical evidence is a single concept (weak or medium tier) is treated as weak, so the floor
  drops it, unless its cosine to the task text is >= tau. Strong evidence (two or more concepts, scope), a whole
  identifier match (the identifier lift runs after the filter and is untouched) and pinned records are
  not filtered. Records that arrive only through graph links have no lexical match and are not filtered.
  It is implemented by wrapping `retriever.classify` for the run; the cosine is to the task text (plus symbols).
  `classify` is not given the record, so the wrapper finds it by walking up the stack one frame
  (`sys._getframe(1)`), checking that the caller is the closure named `passes` and reading its local `ref`;
  that is a measurement hack that breaks if the closure or its parameter is renamed or the call is refactored
  (the script then raises an error rather than silently leaving the tier unfiltered), and a product version
  would pass the ref to `classify` (or move the check into `passes`) explicitly.

Reference: the final retriever without a channel: 0.9875 (158/160), 15.21 noise records and 947.8 tokens per
task, 0/37, 0 leaks. The saved comparison baseline is 1139.4 tokens per task.

### Arm A: SEMANTIC_FLOOR grid (SEMANTIC_K = 20)

| SEMANTIC_FLOOR | Dev recall | Noise records/task | Noise tokens/task | on empty tasks | on non-empty tasks | Clean-abstain | Leaks | In-sample recall / abstention | Held-out |
|---|---|---|---|---|---|---|---|---|---|
| 0.35 | 0.9812 (157/160) | 22.36 | 1390.3 | 1523.2 | 1323.8 | 0/37 | 0 | 30/30, 0/12 | 0/16 |
| 0.45 | 0.9812 (157/160) | 22.36 | 1390.3 | 1523.2 | 1323.8 | 0/37 | 0 | 30/30, 0/12 | 0/16 |
| 0.55 | 0.9812 (157/160) | 22.23 | 1381.3 | 1511.2 | 1316.4 | 0/37 | 0 | 30/30, 0/12 | 0/16 |
| 0.65 | 0.9875 (158/160) | 16.04 | 1010.8 | 802.8 | 1114.8 | 0/37 | 0 | 30/30, 11/12 | 9/16 |
| **0.75** | **0.9875 (158/160)** | **15.17** | **945.9** | **741.1** | **1048.3** | **0/37** | **0** | **30/30, 12/12** | **12/16** |

Reading: with this model the channel only adds records. Cosines between unrelated coding text are 0.4 to 0.65,
so floors up to 0.55 flood: noise rises 47% (1390.3 against 947.8 tokens), the in-sample abstention scenarios
fail (0/12) and so does every held-out abstention (0/16). At 0.65 the channel slightly adds noise on dev (1010.8 against 947.8 tokens, +6.6%) and still
breaks in-sample and held-out cases (11/12, 9/16). At 0.75 almost nothing passes and the result equals the
no-channel run (945.9 against 947.8 tokens, a 0.2% difference). No floor reduces noise or abstains on an empty
task; the two relevant records lost at floors 0.35 to 0.55 (`compact-after-load`, `cursor-paging-fix`) were
pushed out of the 2000-token budget by the extra noise (both are present when the budget is 8000).

### Arm B: confirmation filter, tau grid

| tau | Dev recall | Noise records/task | Noise tokens/task | on empty tasks | on non-empty tasks | Clean-abstain | Leaks | In-sample recall / abstention | Held-out | Extra dev relevant lost |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.3 | 0.9875 (158/160) | 15.21 | 947.8 | 741.1 | 1051.2 | 0/37 | 0 | 30/30, 12/12 | 12/16 | none |
| 0.4 | 0.9875 (158/160) | 15.17 | 945.9 | 741.1 | 1048.3 | 0/37 | 0 | 30/30, 12/12 | 12/16 | none |
| **0.5** | **0.9875 (158/160)** | **14.50** | **903.5** | **707.5** | **1001.5** | **0/37** | **0** | **30/30, 12/12** | **12/16** | **none** |
| 0.6 | 0.9812 (157/160) | 10.59 | 661.0 | 432.4 | 775.4 | 0/37 | 0 | 30/30, 12/12 | 12/16 | `fx-weekend-gap` |
| 0.7 | 0.9563 (153/160) | 7.72 | 493.7 | 247.8 | 616.6 | 5/37 | 0 | 28/30, 12/12 | 15/16 | `declare-grain`, `erasure-request`, `expand-then-contract`, `fx-weekend-gap`, `hmac-tokens` |

Arm B's two rows that bear on the rules. Recall-preserving: tau 0.5, 158/160, noise 903.5 tokens (4.7% below the final
retriever, 21% below the 1139.4 baseline), 0/37. Noise-cutting: tau 0.7, noise 493.7 tokens (48% below the
final retriever, 57% below the baseline), but dev recall falls to 153/160, clean-abstain is 5/37 (0.135), and
two in-sample scenarios fail (28/30: the "write a database migration for the orders table" task_only
scenario and a superseded-condition scenario, "what is our logging convention"); held-out abstention rises to 15/16.
Between 0.5 and 0.7 the recall and noise curves are steep, so a threshold inside that gap is sensitive to the
exact value.

Against the pre-registered acceptance rules, the tau 0.7 row breaks rule 1: recall may not fall, and a fall of
up to 1 point is accepted only with a noise cut of 70% or more. Here recall falls 3.1 points (158/160 to
153/160, above the 1-point allowance) and the noise cut is 57% against the baseline, below 70%. As measured, that
row is therefore not a candidate that satisfies the rules; it also misses rule 4 (clean-abstain 0.135 against
0.80). The tau 0.5 row keeps recall but its 21% cut against the baseline fails rules 3 and 4.

Why it is capped: at 0.7 the filter removes single-concept noise almost completely (858 to 33 noise records
over the 111 tasks, counted from `memory.recall` directly, which differs slightly from the service's
1688 total), but the rest of the noise is untouched strong evidence: scope matches (439 to 442), records
matching two or more words (337 to 348) and graph-linked records (79 to 41). That is why clean-abstain stays low.
Cosine to the task of the relevant records the retriever shows: median 0.70, 10th percentile 0.57, minimum
0.44, 82 of 158 below 0.7. Cosine of the noise records shown: median 0.59, 90th percentile 0.67, maximum 0.78.
The two distributions overlap heavily, so a single tau trades relevant records for noise at about the rate the
table shows.

### Supplementary: with the retrieval instruction in front of the query

bge models recommend the instruction `Represent this sentence for searching relevant passages: ` for queries.
`GraphEmbeddedMemory` does not add it, so the grids above are the headline; this is the same grid with it.

| Arm | Setting | Dev recall | Noise tokens/task | Clean-abstain | In-sample recall / abstention | Held-out |
|---|---|---|---|---|---|---|
| A | floor 0.35 / 0.45 | 0.9812 (157/160) | 1405.1 | 0/37 | 30/30, 0/12 | 0/16 |
| A | floor 0.55 | 0.9812 (157/160) | 1322.8 | 0/37 | 30/30, 3/12 | 1/16 |
| A | floor 0.65 | 0.9875 (158/160) | 965.4 | 0/37 | 30/30, 12/12 | 12/16 |
| A | floor 0.75 | 0.9875 (158/160) | 946.7 | 0/37 | 30/30, 12/12 | 12/16 |
| B | tau 0.3 / 0.4 | 0.9875 (158/160) | 947.8 / 945.2 | 0/37 | 30/30, 12/12 | 12/16 |
| B | tau 0.5 | 0.9875 (158/160) | 875.1 | 0/37 | 30/30, 12/12 | 12/16 |
| B | tau 0.6 | 0.9750 (156/160) | 579.7 | 2/37 | 30/30, 12/12 | 13/16 |
| B | tau 0.7 | 0.9563 (153/160) | 492.8 | 6/37 | 27/30, 12/12 | 16/16 |

Same shape: arm A adds noise or nothing; arm B at tau 0.6 loses `declare-grain` and `fx-weekend-gap`, at 0.7 the
same five records as above, and the in-sample scorecard drops to 27/30 at 0.7.

### Dev relevant records lost

Beyond the two the final retriever already misses (`backfill-billing-night`, `sunday-drop-late`):

| Arm | Records lost |
|---|---|
| A, floors 0.35 to 0.55 | `compact-after-load`, `cursor-paging-fix` (crowded out of the token budget by the extra noise) |
| A, floors 0.65, 0.75 | none |
| B, tau 0.3 to 0.5 | none |
| B, tau 0.6 | `fx-weekend-gap` |
| B, tau 0.7 | `declare-grain`, `erasure-request`, `expand-then-contract`, `fx-weekend-gap`, `hmac-tokens` |

### Cost of the semantic path

| Item | Measured |
|---|---|
| Model load (import + construct, CPU, offline, warm disk cache) | 20.5 s (importing sentence-transformers also loads TensorFlow here) |
| Index per world (embed every active record once) | saas 89 records 1.22 s, pipeline 85 records 0.99 s, mobile 86 records 0.92 s (about 11 ms per record; a 5,000-record store would be about 55 s by extrapolation, not measured) |
| Per-query encode + search, cold query | median 31.8 ms, p95 54.8 ms, max 73.3 ms (the retriever's whole `service.recall` takes a median 2.4 ms on these 90-record worlds, so the channel is about 13 times the lexical path here) |
| Peak process memory (torch, model, TensorFlow, all runs) | about 1.09 GB |
| Dependencies | sentence-transformers and torch (several hundred MB installed) and a cached model of roughly 130 MB; the default product has none of these |

The model files must already be on disk; this run did no download. Records written after attach need
incremental indexing, which the adapter does not do. The query is encoded once per recall (twice when the
service re-asks after an empty result; not measured separately).

### Reproduce

```
PYTHONPATH=src python -m tests.eval.bench.arms --insample --save --query-prefix
```

About 75 s on CPU. It prints every table above, runs the dev split only (the script has no split option), and
`--save` writes `tests/eval/bench/runs/dev-semantic-arms.json` (the settings, summaries, per-setting missed
lists, index and query timings).

### What the numbers say

- The existing additive channel (arm A) with bge-small adds noise at every floor that lets anything through and
  is inert above about 0.75; it never abstains on an empty task and it breaks the in-sample and held-out
  abstention cases at floors up to 0.65. It does not move dev recall (the loss at low floors is budget crowding).
- The confirmation filter (arm B) is the only semantic setting measured here that lowered noise below the
  no-channel run. At tau 0.5 it costs nothing in
  recall and takes 4.7% more off the final retriever's noise; at tau 0.7 it reaches a 57% cut against the
  baseline, with a recall cost of 5 dev records (0.9563) and a clean-abstain of 0.135. Neither row approaches
  the 0.80 clean-abstain target, because most remaining noise is scope, two-word and graph evidence that the
  filter does not touch.
- Overfitting: tau (and the floors) were chosen by looking at the same 111 dev tasks, and the steep part of
  the curve (0.5 to 0.7) sits where a few records decide each point, so the 0.6 and 0.7 figures are
  likely optimistic as estimates of test performance; each recall figure is out of 160 relevant records and
  moves 0.6 points per record. The held-out and in-sample figures are scorecards the earlier levers were also
  tuned against (the held-out 12/16 is the headline figure; its movement to 15/16 or 16/16 at tau 0.7 is a
  small sample, 16 scenarios). The locked test split is the real check and was not run.
- Whether the semantic path is worth its dependency and latency cost is the user's decision; this section only
  states what was measured.

## Final result: test split (locked, run once)

Command, run once after the decision above was written and the configuration frozen (HEAD 56a0b30, no change
to `src/` since):

```
PYTHONPATH=src python -m tests.eval.bench.run --split test --label after --save --against baseline
```

Its output, verbatim:

```
split test, label after
tasks 215 (empty 70)
must-include recall   0.9644  (244/253)  95% CI 0.934-0.981
clean-abstain rate    0.0143  (1/70)  95% CI 0.003-0.077
noise per task        15.009 records, 934.3 tokens
superseded/retracted leaks  0
saved tests/eval/bench/runs/test-after.json
rules against baseline:
  FAIL  recall: 0.9723 -> 0.9644 (noise cut 20%)
  PASS  leaks: 0 leaks
  FAIL  noise: noise tokens 250702 -> 200870 (20% cut)
  FAIL  clean: clean-abstain 0.01
```

(The saved path printed as an absolute path; shortened here.) Saved run: `tests/eval/bench/runs/test-after.json`.

### Test split, baseline against after

| Figure | Baseline | After |
|---|---|---|
| must-include recall | 0.9723 (246/253), 95% CI 0.944-0.987 | 0.9644 (244/253), 95% CI 0.934-0.981 |
| clean-abstain (empty tasks) | 0.0000 (0/70), 95% CI 0.000-0.052 | 0.0143 (1/70), 95% CI 0.003-0.077 |
| noise records per task | 18.865 (4056 in all) | 15.009 (3227 in all), 20.4% fewer |
| noise tokens per task | 1166.1 (250,702 in all) | 934.3 (200,870 in all), 19.9% fewer |
| superseded/retracted leaks | 0 | 0 |

Split by task kind (noise per task, from the saved runs):

| Tasks | Baseline records / tokens | After records / tokens | Token cut |
|---|---|---|---|
| empty (70) | 16.37 / 1014.0 | 12.43 / 770.4 | 24.0% |
| non-empty (145) | 20.07 / 1239.4 | 16.26 / 1013.4 | 18.2% |

Per world:

| World | Tasks (empty) | Recall baseline | Recall after | Clean-abstain baseline / after | Noise tokens per task baseline / after (cut) |
|---|---|---|---|---|---|
| saas | 72 (22) | 84/87 = 0.9655 (CI 0.903-0.988) | 84/87 = 0.9655 (CI 0.903-0.988) | 0/22 / 0/22 | 1172.0 / 951.2 (18.8%) |
| pipeline | 71 (24) | 77/78 = 0.9872 (CI 0.931-0.998) | 75/78 = 0.9615 (CI 0.893-0.987) | 0/24 / 1/24 | 1093.0 / 940.1 (14.0%) |
| mobile | 72 (24) | 85/88 = 0.9659 (CI 0.905-0.988) | 85/88 = 0.9659 (CI 0.905-0.988) | 0/24 / 0/24 | 1232.1 / 911.7 (26.0%) |

The one clean-abstain after is a pipeline task, "export the Jira backlog to a spreadsheet for sprint planning".

### Relevant records missed on the test split

| | Missed (task) |
|---|---|
| Baseline, 7 | `money-in-cents` (what to charge when a customer adds seats halfway through an annual contract); `csv-escape-fix` (importer that reads back a Taskboard CSV export); `search-latency-budget` (quick-find box queries the server on every keystroke); `env-yaml-overlays` (save the Bravo API token in the Airflow connection UI); `bug-blocked-loop` (refused location, take them to the system settings page); `conv-permission-copy` (import the phone's contacts for a group hike); `fix-migrate-crash` (store each track point's speed next to its latitude, longitude, altitude) |
| After, 9 | the same six except `csv-escape-fix` (recovered), plus three new: `english-stemming-quirk` (searching for jane.doe@acme.com returns tasks that only contain 'jane'; saas), `dagbag-test` (CI job that copies dags/ to the scheduler bucket on every merge to main; pipeline), `small-file-explosion` (move the compact task after the quality gate in nightly_ingest; pipeline) |

Net: 6 misses are shared, 1 baseline miss is recovered and 3 new misses appear (7 - 1 + 3 = 9). The 2-record
fall is 0.79 point (0.9723 to 0.9644). The cause of the three new misses was not investigated (whether the
weak-match floor dropped them or the token budget crowded them out); a further run on the test split to find
out would be a second look at the locked set and was not made.

### Acceptance rules (spec section 6), measured once on the test split

| # | Rule | Result | Figures |
|---|---|---|---|
| 1 | Must-include recall does not fall (a fall of up to 1 pt only with a noise cut of 70% or more) | FAIL | 0.9723 to 0.9644 (-0.79 pt, 2 records); noise cut 20%, below the 70% that a fall requires |
| 2 | Superseded and retracted leaks stay 0 | PASS | 0 leaks |
| 3 | Mean noise tokens per task fall by at least 50% against the baseline | FAIL | 1166.1 to 934.3 (250,702 to 200,870 tokens in all), a 19.9% cut |
| 4 | Clean-abstain on empty tasks reaches 0.80 | FAIL | 1/70 = 0.0143, Wilson 95% CI 0.003-0.077 (baseline 0/70) |
| 5 | `test_recall_latency_guard` passes and the 5,000-record median is reported beside the baseline | PASS | the guard test passes (best of three p95 under 250 ms); median recall 43.9 and 43.6 ms against the recorded baseline 36.3 and 35.5 ms, see below |
| 6 | No existing scenario, expectation or held-out label changes | PASS | `git diff 933068e..HEAD --stat -- tests/eval/scenarios.py tests/eval/world.py tests/eval/heldout.py tests/eval/harness.py tests/eval/run.py` is empty (933068e is `git merge-base main HEAD`, the last commit before the first `src/` change) |
| 7 | The full default suite passes | PASS | see the full-suite line below |

Rules 1, 3 and 4 failed. The rules were not moved. Rule 5 passes as written (the guard and the reporting); the
latency figure is a cost, read below.

### Latency, 5,000 records (median recall, final retriever)

The same script as the baseline measurement (`_five_thousand`, 40 recalls, median), each run after a rest of
about 55 seconds:

| Run | Median |
|---|---|
| final retriever, first | 43.9 ms |
| final retriever, second | 43.6 ms |
| recorded baseline (Task 12) | 36.3 ms, 35.5 ms |

To rule out a slower machine state, the unchanged retriever (`git archive 933068e src`, extracted to a scratch
directory) was timed in the same session, alternating with the final one, each after a rest of about 50 seconds:

| Pair | Baseline code | Final code |
|---|---|---|
| 1 | 34.9 ms | 46.6 ms |
| 2 | 35.3 ms | 44.3 ms |

The final retriever's median is about 9 to 12 ms higher at 5,000 records, 25 to 34% above the baseline code
measured the same way. The figures are consistent between runs and no throttling was visible (the minimum was
within 3 ms of the median in each run). The guard test passes with a wide margin (its bound is 250 ms).

### In-sample, held-out (the guards the levers were tuned against)

| Figure | Baseline | After |
|---|---|---|
| In-sample scorecard (`python -m tests.eval.run --service`) | 50 scenarios, all conditions pass; recall 30/30; leaks 0; abstention 12/12 | same: all conditions pass; recall 30/30; leaks 0; abstention 12/12 |
| Held-out abstention, original labels (headline) | 0.38 (6/16) | 0.75 (12/16) |
| Held-out abstention, relabelled after review (not substituted) | 0.50 (8/16) | 0.9375 (15/16) |

Held-out failures after: "refactor the logging setup for the CLI", "improve error messages in the onboarding
wizard", "migrate the test runner to pytest-xdist", "configure the sentry error reporting DSN" (three of them
are the relabelled tasks). The in-sample and held-out sets were used while tuning the levers, so these are not
independent of the final configuration.

### Dev against test

| | Dev (tuned on) | Test (locked) |
|---|---|---|
| Baseline recall | 0.9875 (158/160) | 0.9723 (246/253) |
| After recall | 0.9875 (158/160) | 0.9644 (244/253) |
| Baseline noise tokens per task | 1139.4 | 1166.1 |
| After noise tokens per task | 947.8 | 934.3 |
| Noise-token cut | 16.8% | 19.9% |
| Clean-abstain after | 0/37 | 1/70 |

### Reading

The retriever as built removes about a fifth of the noise tokens on the locked test split (19.9%, against 16.8%
on dev), where the rule asks for half, and it abstains on 1 of 70 empty tasks, where the rule asks for four in
five. Dev showed the same shape before the test run, so the miss is not a test-split surprise: the levers
were never close to those two rules on dev. The cut was spread evenly rather than concentrated (empty tasks
24%, non-empty 18%; 14% to 26% across the three worlds). Recall fell by two records (0.79 point), within the
1-point allowance only for a noise cut of 70% or more, which was not reached, so rule 1 fails on its own terms.
The change cost 3 relevant records and recovered 1. Leaks stayed at 0, the guard and the full suite pass, and no
existing scenario or label changed. The latency at 5,000 records is higher than the baseline's by about a
quarter to a third. Held-out abstention rose from 0.38 to 0.75 (original labels) and 0.50 to 0.9375
(relabelled), and the in-sample scorecard is unchanged, but those sets guided the tuning.

The remaining noise is ordinary topic words and scope matches that, to a lexical retriever, look like real
matches; the semantic arm on dev (above) removes more noise only at a recall cost, and was measured on dev
only. The decision about embeddings as a default is the user's; this document states what was measured.

Full default suite (`python -m pytest tests -q -p no:cacheprovider`): 1231 passed, 7 skipped, 8 deselected (86.8 s). The first full run (140.7 s, 1230 passed) failed one test, `test_recall_latency_guard`; that run took 54 s longer than the second, which fits the CPU throttling described in `tests/eval/RESULTS.md` (its assertion message was not captured). The guard passed when run alone twice (6.4 s and 6.2 s) and the full suite passed on the second run, so rule 7 is recorded as PASS, with the one flake disclosed. The suite includes the new `tests/eval/test_bench_dev.py` (3 passed).

## Known limits

- The worlds were written by us, in a style we know. The benchmark is a better estimator than 16 tasks, not a
  measurement of real use.
- Locking the test split is a discipline made visible by a checksum, not a technical barrier.
- The thresholds (RARE_SHARE, SMALL_STORE, RARE_CAP, the generic-word list) were set on the dev split, which
  is also what the in-sample and held-out figures were tuned against. Compare the change, not the levels
  (the levels differ because the two splits differ): the baseline-to-final change in recall was 0 on dev
  (158/160 both) and -0.79 point on test (246/253 to 244/253).
- Lever 7 (RARE_CAP) was added after the dev results of levers 1-3 were seen. It is not pre-registered. It
  was judged on the test split with everything else.
- The cap does not scale with the store: RARE_CAP is an absolute 10 records however many records there are,
  and the benchmark holds 75 to 100 records per world, so its effect on very large stores is untested.
- Several labels are borderline (listed under "Known borderline labels"). They were not corrected, so some of
  the "noise" counted may be acceptable, and some "relevant" records may be arguable.
- Lexical ceiling: the remaining noise is ordinary topic words (`call`, `file`, `screen`, `customer`, `test`) and
  scope matches. A lexical retriever cannot tell them from real matches without a semantic signal. The semantic
  arm numbers are in the "Semantic arm" section (dev only: arm A adds noise or nothing, arm B cuts noise
  only at a recall cost and reaches clean-abstain 0.135 at best); they were not measured on the test split.
  Whether to make embeddings a default is the user's decision.
- The three new test-split misses were not diagnosed (see above).

## Final review findings (retrieval levers)

Added after the whole-branch review, which ran after the test-split result above. The review recommends against
merging the retrieval levers as configured. Nothing below is fixed: `src/` stays exactly as measured on the test
split (ruling R16), so the defects are documented, not repaired. The reviewer's evidence:

1. Critical: `RARE_CAP = 10` drops every single-word match on a topic that more than 10 notes hold, at any store
   size. Reproduced: in a store of 300 records with 12 notes about "ledger", the task "ledger" returns 12 notes
   on `main` and 0 on the branch; "how do we handle the ledger" returns 12 against 0. In a 5,000-record store
   "rare" means at most 0.2% of the records. Spec 5.1 had chosen relative document frequency (a share of the
   store), not an absolute count. The benchmark worlds hold 75 to 100 records, so the benchmark cannot see this.
   Lever 7 also passed the R2 in-sample gate only because it depends on store size: it does not bind at the
   40-record in-sample world but does bind at about 90 records.
2. Important: lever 2 (graph seeds from non-weak survivors) regresses small stores. A fix linked to a weakly
   matched bug in an 8-record store is shown on `main` and not on the branch; `tests/test_evidence_floor.py` pins
   that as intended. Lever 2 had zero measured effect on the benchmark, and spec section 7 says a lever that does
   not earn its place is dropped, not left in. It was left in.
3. Important: the identifier lift covers camelCase only, but the service passes ALL-CAPS and alphanumeric codes
   (`ECONNRESET`, `E11000`, `JWT`) and the parts of snake_case names as symbols. Reproduced: 30 of 300 records
   mention `ECONNRESET`; `main` returns 30, the branch 0.
4. Important: latency. `passes()` now does per-candidate work (a set difference, `_concepts`, a `min` over
   document frequencies, a kwargs dict, `classify`) for every fused candidate. A cProfile over 40 recalls on the
   5,000-record probe shows `passes` at 4.0 s of `_rank`'s 14.6 s. The reviewer suggests a cheap fix: return
   STRONG for scope and semantic hits before any work, memoise the lexical tier by the `matched` frozenset, and
   cache record compound tokens on the index. It was not applied, because `src/` is frozen. The 25-34% slowdown
   measured above depends on the workload: a service-level probe with 5,000 conventions showed no difference in
   one session.

Also on record:

- The dev-to-test gap is the change, not the levels: the baseline-to-final recall change was 0 on dev and
  -0.79 point on test (see "Known limits").
- Lever 4 (the tail) was not built under the pre-registered rule and can no longer be judged on this test split.
- The three new test misses (`english-stemming-quirk`, `dagbag-test`, `small-file-explosion`) were deliberately
  not diagnosed, to keep the split clean.
- Reviving the levers needs the fixes above plus a fresh locked test split, because this one has now been seen
  against this lever family.

Merge shape the controller will give the user: merge the benchmark, the runner, the locks, the results and the
docs; hold the retrieval levers (the `src/agent_memory/retrieval/evidence.py` and `retriever.py` changes,
`tests/test_evidence*.py` and the README recall note) on a separate branch. If the levers are held back,
`tests/eval/test_bench_dev.py` has to be re-pinned: it asserts the levers' dev numbers (noise at most 1090
tokens per task) and would need the baseline figure (1139.4 tokens per task) instead.

## Known world inconsistencies (not edited: editing a world changes what the locked labels mean)

Found in the Task 4 and 5 reviews and deferred. The worlds are locked by `bench.sha256`; none of these was
corrected.

- Pipeline world: `sunday-drop-late` (the sensor window against 06:40); `dags-in-utc` (March against October);
  the `revenue-drift` module; the `monday-freshness` anchor; the `shuffle-partitions-400` arithmetic; the
  `dim-bloat` module.
- Mobile world: `bug-migrate-crash` (the retry loop against what `db.ts` does); missing down steps in the
  migrations; the `defusedxml` comment; the Fastfile phased rollout; `dp-download-queue` (serial against 4
  parallel); the `conv-release-name` unit test; a missing fixtures directory; the `dec-gpx-11` anchor; a
  mixed pre-fix and post-fix file state.
