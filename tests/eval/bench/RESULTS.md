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
