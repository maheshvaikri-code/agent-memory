"""Print the scorecard: PYTHONPATH=src python -m tests.eval.run"""
import json
import sys
import tempfile
from pathlib import Path

from agent_memory import AgentMemory

from .harness import evaluate
from .world import build_world


def make_fns(memory):
    if hasattr(memory, "recall"):
        recall_fn = lambda task, files: memory.recall(task=task, files=files, max_tokens=2000).to_prompt()
        check_fn = lambda approach, files: "\n".join(
            f"- [{w.record.id}] {w.text}" for w in memory.check(approach=approach, files=files))
    else:
        recall_fn = lambda task, files: memory.get_context(task=task, files=files, max_tokens=2000).to_prompt()
        check_fn = lambda approach, files: json.dumps(memory.check_suggestion(approach))
    return recall_fn, check_fn


def build(root: Path):
    try:
        memory = AgentMemory("shopline", repo_root=str(root))
    except TypeError:
        memory = AgentMemory("shopline")
    build_world(memory, root)
    return memory


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        memory = build(Path(tmp))
        card = evaluate(*make_fns(memory))
    out = card.to_markdown()
    if card.failures:
        out += "\n\nFailures:\n" + "\n".join(f"- {f}" for f in card.failures)
    sys.stdout.buffer.write((out + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
