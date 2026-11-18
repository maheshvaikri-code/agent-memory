"""Print the scorecard: PYTHONPATH=src python -m tests.eval.run [--shared] [--service]

--shared runs the same evaluation with every AgentMemory backed by a shared store
(AgentMemory.open), the serial-equivalence check of the concurrency-safe storage.
--service asks through MemoryService (the boundary the MCP server and the CLI use) and judges
the prompt it returns: the untrusted-notes wrapper, escaping and token clamp included.
"""
import contextlib
import json
import sys
import tempfile
from pathlib import Path

from agent_memory import AgentMemory

from .harness import evaluate
from .heldout import HELDOUT
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


@contextlib.contextmanager
def _mode(shared: bool):
    """Plain AgentMemory(...), or every instance backed by a shared store in its own folder."""
    if not shared:
        yield
        return
    from tests.shared_mode import force_shared
    with tempfile.TemporaryDirectory() as stores, force_shared(stores):
        yield


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    shared = "--shared" in argv
    with _mode(shared), tempfile.TemporaryDirectory() as tmp:
        memory = build(Path(tmp))
        if "--service" in argv:
            from agent_memory.service import MemoryService

            from .eval_service import service_fns
            fns = service_fns(MemoryService(memory=memory))
        else:
            fns = make_fns(memory)
        card = evaluate(*fns)
        held = evaluate(*fns, scenarios=HELDOUT)
        getattr(memory, "close", lambda: None)()      # shared mode: checkpoint, release
    out = card.to_markdown()
    if card.failures:
        out += "\n\nFailures:\n" + "\n".join(f"- {f}" for f in card.failures)
    out += ("\n\nHeld-out abstention (scenarios never used for tuning): "
            f"{held.abstention:.2f} ({held.abstain_hits}/{held.abstain_total})")
    if held.failures:
        out += "\n\nHeld-out failures:\n" + "\n".join(f"- {f}" for f in held.failures)
    sys.stdout.buffer.write((out + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
