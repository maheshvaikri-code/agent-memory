"""Run the recall benchmark: PYTHONPATH=src python -m tests.eval.bench.run --split dev|test

  --label NAME     name for the saved run (default "run")
  --save           write tests/eval/bench/runs/<split>-<label>.json
  --against NAME   compare with runs/<split>-NAME.json and print acceptance rules 1-4
"""
import dataclasses
import json
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

from . import judge, model
from .build import build_world
from .stats import wilson

RUNS = model.BENCH / "runs"


def run_split(split: str) -> List[judge.TaskResult]:
    tasks = model.load_tasks(split)
    results: List[judge.TaskResult] = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in model.WORLDS:
            mine = [t for t in tasks if t.world == name]
            if not mine:
                continue
            built = build_world(model.load_world(name), Path(tmp) / name)
            for t in mine:
                prompt = built.service.recall(t.task, t.files, max_tokens=2000)["prompt"]
                results.append(judge.judge(t, prompt, built.key_of, built.world))
            getattr(built.memory, "close", lambda: None)()
    return results


def rules(before: Dict, after: Dict) -> List[Tuple[str, bool, str]]:
    """Acceptance rules 1-4 of the spec, from two saved summaries."""
    def recall(s):
        return s["relevant_hits"] / s["relevant_total"] if s["relevant_total"] else 1.0

    def clean(s):
        return s["clean"] / s["empty_tasks"] if s["empty_tasks"] else 1.0

    cut = 1.0 if not before["noise_tokens"] else 1 - after["noise_tokens"] / before["noise_tokens"]
    fall = recall(before) - recall(after)
    recall_ok = fall <= 1e-9 or (fall <= 0.01 + 1e-9 and cut >= 0.70)
    return [
        ("recall", recall_ok, f"{recall(before):.4f} -> {recall(after):.4f} (noise cut {cut:.0%})"),
        ("leaks", after["leaks"] == 0, f"{after['leaks']} leaks"),
        ("noise", cut >= 0.50, f"noise tokens {before['noise_tokens']} -> {after['noise_tokens']} ({cut:.0%} cut)"),
        ("clean", clean(after) >= 0.80, f"clean-abstain {clean(after):.2f}"),
    ]


def _table(summary: judge.Summary) -> str:
    rlo, rhi = wilson(summary.relevant_hits, summary.relevant_total)
    clo, chi = wilson(summary.clean, summary.empty_tasks)
    return "\n".join([
        f"tasks {summary.tasks} (empty {summary.empty_tasks})",
        f"must-include recall   {summary.recall:.4f}  ({summary.relevant_hits}/{summary.relevant_total})"
        f"  95% CI {rlo:.3f}-{rhi:.3f}",
        f"clean-abstain rate    {summary.clean_rate:.4f}  ({summary.clean}/{summary.empty_tasks})"
        f"  95% CI {clo:.3f}-{chi:.3f}",
        f"noise per task        {summary.noise_per_task:.3f} records, "
        f"{summary.noise_tokens_per_task:.1f} tokens",
        f"superseded/retracted leaks  {summary.leaks}",
    ])


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)

    def option(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    split = option("--split")
    if split not in model.SPLITS:
        print(__doc__)
        return 2
    label = option("--label", "run")
    results = run_split(split)
    summary = judge.summarise(results)
    out = [f"split {split}, label {label}", _table(summary)]
    if "--save" in argv:
        RUNS.mkdir(exist_ok=True)
        path = RUNS / f"{split}-{label}.json"
        payload = {"label": label, "split": split, "summary": dataclasses.asdict(summary),
                   "tasks": [dict(world=r.world, task=r.task, empty=r.empty, shown=r.shown,
                                  missed=r.missed, noise=r.noise, noise_tokens=r.noise_tokens,
                                  leaks=r.leaks) for r in results]}
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=1) + "\n")
        out.append(f"saved {path}")
    against = option("--against")
    if against:
        before = json.loads((RUNS / f"{split}-{against}.json").read_text(encoding="utf-8"))["summary"]
        out.append(f"rules against {against}:")
        for name, ok, detail in rules(before, dataclasses.asdict(summary)):
            out.append(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
