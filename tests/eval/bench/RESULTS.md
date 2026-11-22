# Benchmark results

Baseline: the unchanged retriever (branch feature/recall-quality, no retrieval code changed), run with
`PYTHONPATH=src python -m tests.eval.bench.run --split dev|test --label baseline --save`.
Saved runs: `tests/eval/bench/runs/dev-baseline.json`, `tests/eval/bench/runs/test-baseline.json`.
The test split was run once, as this baseline; it is not rerun until the final result.

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

48 scenarios, all conditions pass (task_only 6/6, files_only 6/6, absent 6/6, irrelevant 6/6,
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

## Label corrections

none

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

The difference is 4.5 tokens per task (0.4% of noise), far below the 3-point threshold, so the spec
reading ships. The same change makes the whole-identifier rule test the informative terms: a generic word
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
fail (0/12) and so does every held-out abstention (0/16). At 0.65 the channel is mostly inert on dev but still
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

Arm B's two useful rows. Recall-preserving: tau 0.5, 158/160, noise 903.5 tokens (4.7% below the final
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
- The confirmation filter (arm B) is the only semantic use that reduces noise. At tau 0.5 it costs nothing in
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
