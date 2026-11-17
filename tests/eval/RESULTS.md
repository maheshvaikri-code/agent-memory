# Results after the retrieval rework

Run on 2026-11-16 with `PYTHONPATH=src python -m tests.eval.run`:

| Condition | Passed |
|---|---|
| task_only | 6/6 |
| files_only | 6/6 |
| absent | 6/6 |
| irrelevant | 6/6 |
| adjacent | 6/6 |
| superseded | 6/6 |
| stale | 6/6 |
| guardrail | 6/6 |

- must-include recall: 1.00 (28/28)
- superseded or retracted leaks: 0
- abstention: 1.00 (12/12)

## Baseline versus result

| Metric | Baseline (`BASELINE.md`) | After the rework |
|---|---|---|
| must-include recall | 0.39 (11/28) | 1.00 (28/28) |
| superseded or retracted leaks | 0 | 0 |
| abstention | 0.50 (6/12) | 1.00 (12/12) |

## Latency

All figures are at 5,000 `quirk` records, each of which matches the query
words "misbehaves", "large" and "input", so every recall ranks 5,000
survivors. Each row is a separate fresh process, and each figure is the
median of three runs.

| Path | p95 | median |
|---|---|---|
| warm and read-only: the gate test's body, 40 recalls, no writes | 36.5, 36.9, 37.6 ms | 30 ms |
| cold: the first recall after loading (decodes and indexes everything) | 144-147 ms (single sample) | - |
| one `graph.update_node` before each recall (40 recalls) | 38.8, 40.9, 38.6 ms | 32 ms |
| one `remember()` before each recall (20 recalls) | 39.6, 42.4, 40.5 ms | 31 ms |
| `check()`, warm (40 calls) | 27.1, 27.8, 27.7 ms | 19 ms |

`test_p95_latency_at_five_thousand_records` measures only the warm,
read-only path. Its bound is 50 ms. Inside pytest the same body measured p95
30.6, 38.0 and 33.5 ms.

The cold first recall is over 50 ms. A process pays it once.

After a write, the BM25 index is updated in place
(`BM25.update`, `_Index.refresh`). Before that change, every write forced a
full rebuild, and a recall after a write measured median about 60 ms and p95
78-100 ms.

Before the performance work, the gate test measured p95 5,863 ms because of
pairwise de-duplication over every survivor.

**Measurement caveat.** These are wall-clock times on one Windows 11 laptop
with CPython 3.12. This machine drops to about a third of its speed after
roughly 3 seconds of sustained load, and also when the load is intermittent
(sleeping between recalls tripled every figure). A pure-Python loop with no
project code shows the same 2.5-3x step. All figures above were taken inside
that first window. A run that crosses into the slow state reports p95 of
70-100 ms for code that measures about 37 ms otherwise. While the machine was
in that state, the gate test also failed inside the full suite. That happened
both for this code and for the earlier commit bd5544b. The two commits
measured the same when compared one after the other.

## Notes

- No scenario was changed. The last two abstention failures, the `absent`
  task "add a dark mode toggle to the settings screen" and the `irrelevant`
  task "adjust editor indentation settings", had one cause: the stemmer
  reduced "settings" to "set", which matched the verb in "http cache headers
  on landing assets are set immutable". Now a plural "-ings" word drops only
  its plural ("settings" becomes "setting", a noun). Short roots still collapse
  ("logging", "logged" and "log" all become "log").
- All six guardrail scenarios pass on lexical matching plus file scope. The
  open risk in the spec is unchanged: an approach that is a pure paraphrase,
  sharing no words and no file with the stored guardrail, still needs the
  semantic channel.
