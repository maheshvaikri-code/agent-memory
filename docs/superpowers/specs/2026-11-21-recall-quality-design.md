# Recall quality (sub-project 4): design

Status: implemented; the acceptance rules were measured once on the locked test split, see tests/eval/bench/RESULTS.md (which rules passed and which failed is stated there). Written 2026-11-21. Builds on the retrieval and record model (`2026-11-16-retrieval-and-record-model-design.md`); its results file is `tests/eval/RESULTS.md`.

## 1. Purpose and success

Recall must stay quiet on a task the memory knows nothing about, and must still return every record a task needs. Today it does not stay quiet: on 16 held-out tasks, 10 return a record the task has no use for (held-out abstention 0.38, 6/16). The retrieval spec set "best in the market" as a measurable target. This sub-project makes that measurable on a benchmark that no tuning has touched, and then improves recall against it.

It succeeds when, on the locked test split (section 4), every rule in section 6 holds. A rule that fails is reported as failed; the rule is not moved.

Two deliverables, in this order:

1. A benchmark that can tell a better retriever from one tuned to its own tests.
2. Retrieval changes judged on that benchmark.

## 2. What the diagnosis found

The 10 failing held-out tasks, classified by what they returned (run 2026-11-21 on `main` at 933068e):

| Returned record | Tasks | Verdict |
|---|---|---|
| Logging convention for "refactor the logging setup" | 1 | A correct match, mislabelled as a failure |
| `ApiError` convention for "improve error messages" and "configure the sentry error reporting DSN" | 2 | Arguably useful |
| Unrelated records matched on one generic stem: `writ`, `handl`, `set`, `document`, `process`, `export` | 6 | Real noise |
| "migrate the test runner" matching the migration runner | 1 | One word, two meanings |

Findings that shape the design:

- The noise and the real matches score alike. Noise scores 0.002 to 0.015; the legitimate logging match scores 0.0115. No score threshold separates them. This agrees with the floor experiment in `RESULTS.md`.
- A graph hop spreads the noise: the unrelated migrations tech-debt note arrives linked from a seed that was itself noise.
- The benchmark is too small to tune against. The world holds about 40 records and the held-out set 16 tasks. `RESULTS.md` already concluded that a lexical floor cannot separate the cases "without a larger corpus where document frequency means something".
- The held-out labels are binary (empty or not), which counts a correct match as a failure.

## 3. Decisions taken with the user

| # | Decision | Chosen over |
|---|---|---|
| 1 | Benchmark first. The dependency decision (embeddings in the default path) is made from measured numbers, in this spec's results, not up front. | Zero-dependency lexical only; ship an embedding model by default. |
| 2 | The semantic channel stays optional in this sub-project. Making it a default is a separate decision, taken with the numbers in hand. | Deciding it now. |
| 3 | Acceptance rules (section 6) are fixed before any retrieval change is run. | Choosing thresholds after seeing results. |

## 4. The benchmark

### 4.1 Worlds

Three hand-written domains of about 80 to 100 records each, committed as data files under `tests/eval/bench/`, not generated: a web SaaS app, a data pipeline, and a mobile/CLI tool. Records span every kind the product stores (decisions, alternatives, conventions, bug patterns, fixes, tech debt, refactoring attempts, superseded and retracted notes) with realistic anchors and scopes. Each world has its own file tree.

The three domains share generic vocabulary ("process", "handle", "document", "runner", "export", "set") on purpose, so document frequency and word sense mean something. A task is asked against one world at a time. The existing 40-record `shopline` world and its 50 scenarios stay as they are.

### 4.2 Tasks and labels

Each task has a description, a file list, and a label per record:

- `relevant`: must appear;
- `acceptable`: fine to show, not required;
- every other record is noise.

A task with no `relevant` records is an empty task: the right result is nothing beyond the pinned set and `acceptable` records. About a third of the tasks are empty. Task authors write the labels, not the retriever. Authors are given the world and told to write natural tasks; they are not shown the retriever code or its results.

### 4.3 Locked split

- **Dev**: about 100 tasks. All tuning uses these.
- **Test**: about 200 tasks, written and committed before any retrieval change, in a commit of their own. A checksum test fails if the file changes afterwards, so an edit is visible in review. The test split is not part of the default `pytest` run. A separate command runs it, and its results are recorded at milestones only: once before any retrieval change and once after the last one.
- A label found wrong after the fact is not corrected in place. It is listed in the results file, and the corrected figure appears beside the original.

### 4.4 Metrics

Every figure is reported as counts, not only a rate; test-split rates carry a 95% confidence interval.

- must-include recall: `relevant` records shown over `relevant` records;
- noise per task: mean noise records shown and mean noise tokens shown;
- clean-abstain rate: empty tasks that returned nothing beyond the pinned set and `acceptable` records;
- superseded and retracted leaks (must be 0);
- recall latency at 5,000 records.

The judge reads the rendered prompt through `MemoryService`, the boundary the adapters use, so wrapper, escaping and token clamp are included. Rendered lines carry record ids, which is what the labels name.

### 4.5 The existing held-out set

The original 0.38 (6/16) stays reported unchanged. A relabelled figure (logging and `ApiError` records marked `acceptable`) is printed beside it, marked as relabelled after the fact. Relabelling is not substituted for the original.

### 4.6 Arms

Every run compares three arms on the same tasks: the current retriever (the baseline); the candidate lexical retriever; and the candidate with the optional semantic channel on, using one pinned small model. The semantic arm runs only when the embeddings package is installed; the default test run never needs it.

## 5. Retrieval changes

Today a record survives the floor on any single shared stem. The change ranks the evidence behind a match and drops records that have only weak evidence. It is made inside the floor in `src/agent_memory/retrieval/retriever.py` (the `passes` rule) and its candidate helpers; the fusion, modifiers, de-duplication and budget code do not change.

### 5.1 Evidence tiers

- **Strong**: two or more distinct matched concepts (counted as `_concepts` counts them), a scope hit of at least `SCOPE_FLOOR` (0.6), a semantic hit, or a graph hop from a strong or medium seed.
- **Medium**: one matched concept that is rare in the corpus. Rarity is relative document frequency (the share of active records containing the term), not the current absolute `DISTINCTIVE_DF`. The earlier floor experiment failed because in a 40-record world nearly every term looked rare.
- **Weak**: one matched concept that is common, or a generic task verb or noun (5.2).

### 5.2 Levers

Each is tried on its own, on the dev split only, and every trial is recorded in a table in the results file.

1. **Drop weak-only records from `recall()`.** `check()` keeps its stricter floor.
2. **Graph seeds from strong and medium survivors only.** Today any survivor seeds, which pulls the noise's neighbours in.
3. **A committed list of generic task words** ("write", "handle", "configure", "refactor", "improve", "export", "process", "document", and similar) that always count as weak when they are the only match. The list is written from general knowledge before the test split is first run, and is not edited against test results. A word in the list still counts toward a record that has a second matched concept.
4. **Safety valve, run as its own arm**: weak matches go to a capped "possibly related" tail (at most 2 items and 10% of the token budget) instead of being dropped. This protects the rare real single-word match, such as "add logging to the order handler" reaching the structlog convention through `log` alone. Tail items count as shown, so they count against noise in the metrics. The numbers choose between dropping and the tail.
5. **Small-store rule.** In a store with fewer than N active records (N chosen on dev, 25 proposed) the weak tier is not dropped: with few records the noise cost is small and a real match is worth more. N is a constant in one place.
6. **Ruled out in advance**: a rank-relative score cut. Section 2 shows the scores cannot separate noise from real matches; the dev trial confirms this and the lever is not shipped.

### 5.3 Semantic arm

The existing optional channel is measured on the same dev split with one pinned small model, and its effect is reported. The default path stays zero-dependency in this sub-project. The report ends with a recommendation on whether the semantic channel should become a default, and that decision is left to the user.

### 5.4 Unchanged

The record model, the storage format, the tool surface (MCP, CLI), the protocol block and the installer goldens do not change. If the tail (5.2, lever 4) is adopted, the rendered prompt gains one labelled section and `docs` and the protocol block are updated in the same change; if it is not adopted, no output format changes.

## 6. Acceptance rules

Fixed before any retrieval change is run. Measured on the test split, one run before and one after.

1. Must-include recall does not fall. A fall of up to 1 percentage point is accepted only if rule 3 is beaten by a wide margin (noise tokens fall by 70% or more); a larger fall fails the rule.
2. Superseded and retracted leaks stay at 0.
3. Mean noise tokens per task fall by at least 50% against the baseline arm.
4. Clean-abstain rate on empty tasks reaches at least 0.80.
5. The 5,000-record latency guard (`test_recall_latency_guard`) still passes, and the median recall at 5,000 records is reported beside the baseline.
6. No existing scenario, expectation or held-out label changes.
7. The full default suite passes.

## 7. Process and tests

- Dev trials are cheap and local; the test split is run twice in total.
- Retrieval changes land one lever per commit, each with the dev numbers in the commit body and in the results table, so a lever that does not earn its place is dropped, not left in.
- Unit tests cover the tier logic (each tier from a small crafted corpus), the small-store rule at its boundary, graph seeding from medium and strong only, the generic-word list as an exact set, and the tail's caps if adopted.
- The benchmark loader and judge have their own tests: label parsing, the checksum guard, the metric arithmetic on a tiny fixed run, and the confidence interval.
- Review focus for the plan: the weak tier must never drop a `relevant` record on a task that names a file in scope; a pinned record is unaffected; `check()` is unchanged; results do not depend on dict order or on the order records were written.

## 8. Out of scope

- Making embeddings a default dependency (a recommendation only).
- Changing fusion weights, recency, tier boosts or feedback.
- The worker-thread follow-up for the remote server.
- A benchmark of real user stores; the worlds are written, not collected.

## 9. Known limits

- The worlds are written by us, in a style we know. The benchmark is a better estimator than 16 tasks, not a measurement of real use.
- Locking the test split is a discipline made visible by a checksum, not a technical barrier.
- Word sense ("runner") is only partly addressable lexically; the semantic arm is how this spec measures the rest.

## 10. Results

Measured once on the locked test split (215 tasks, 70 empty) with the final retriever, against the baseline
run of the unchanged retriever. Full tables, per-world figures, the lever trials and the decision are in
`tests/eval/bench/RESULTS.md`.

| # | Rule | Result | Figures |
|---|---|---|---|
| 1 | Must-include recall does not fall | FAIL | 0.9723 (246/253) to 0.9644 (244/253); the 0.79 point fall is allowed only with a 70% noise cut, the cut was 20% |
| 2 | Leaks stay 0 | PASS | 0 |
| 3 | Noise tokens per task fall by 50% | FAIL | 1166.1 to 934.3, a 19.9% cut |
| 4 | Clean-abstain reaches 0.80 | FAIL | 1/70 = 0.0143 (95% CI 0.003-0.077) |
| 5 | Latency guard passes, median at 5,000 records reported | PASS | guard passes; median 43.9 and 43.6 ms against the baseline's 36.3 and 35.5 ms (the unchanged code measured 34.9 and 35.3 ms in the same session) |
| 6 | No existing scenario, expectation or held-out label changes | PASS | the diff of the scenario, world, held-out, harness and runner files is empty |
| 7 | The full default suite passes | PASS | 1231 passed on the second full run; the first run failed the latency guard once under load (it passes alone), see `tests/eval/bench/RESULTS.md` |

Reading. Three of the seven rules failed: recall fell by two records, noise tokens fell by a fifth instead of a
half, and the retriever abstained on one empty task in seventy. The rules were not moved. Dev showed the same
shape before the test run (noise cut 16.8%, clean-abstain 0/37), so the levers never came near rules 3 and 4;
the remaining noise is ordinary topic words and scope matches that a lexical retriever cannot tell from
real matches. Recall equals the baseline's on dev and is 0.79 point lower on test; the retriever never
leaks a superseded or retracted note, and raises held-out abstention from 0.38 (6/16) to 0.75 (12/16)
(relabelled 0.50 to 0.9375), figures the levers were tuned against. Recall latency at 5,000 records is about a
quarter to a third higher than the baseline's. Of the levers, the tail (lever 4) was not built because the
pre-registered rule is decided by its first condition (dev drop-recall equals the baseline's), lever 6 was
ruled out and measured, and lever 7 was added after the dev results of levers 1-3. The semantic arm was
measured on dev only (section 5.3 and the bench results); whether embeddings should become a default is
left to the user, and this section does not recommend either way.
