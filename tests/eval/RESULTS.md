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

`test_p95_latency_at_five_thousand_records`: 5,000 `quirk` records, all of
which match the query words "misbehaves", "large" and "input", so every recall
ranks 5,000 survivors. The body of the test was run three times as a one-off
`python -c` script (outside pytest):

| Run | p95 | median |
|---|---|---|
| 1 | 35.9 ms | 28.8 ms |
| 2 | 36.3 ms | 28.9 ms |
| 3 | 35.4 ms | 29.1 ms |

Inside pytest the same body measured p95 29.3, 30.0 and 30.6 ms. The bound is
50 ms. Before the performance work the same test measured p95 5,863 ms
(pairwise de-duplication over every survivor).

The numbers are wall-clock times on one Windows 11 development machine with
CPython 3.12. They will vary with hardware.

## Notes

- No scenario was changed. The last two abstention failures, the `absent`
  task "add a dark mode toggle to the settings screen" and the `irrelevant`
  task "adjust editor indentation settings", had one cause: the stemmer
  reduced "settings" to "set", which matched the verb in "http cache headers
  on landing assets are set immutable". The stemmer no longer undoubles a stem
  below four letters ("settings" becomes "sett").
- All six guardrail scenarios pass on lexical matching plus file scope. The
  open risk in the spec is unchanged: an approach that is a pure paraphrase,
  sharing no words and no file with the stored guardrail, still needs the
  semantic channel.
