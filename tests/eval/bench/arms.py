"""Measurements for the recall benchmark, dev split only; nothing here changes src/.

Reproduce (from the repository root; offline, CPU; needs sentence-transformers and torch, and the
model BAAI/bge-small-en-v1.5 in the local Hugging Face cache):

    PYTHONPATH=src python -m tests.eval.bench.arms [--insample] [--save] [--query-prefix]

It runs, against the current final retriever, on the dev split only (there is no split option):
  lever 6  a rank-relative score cut: after the floor, non-pinned items scoring below a fraction f
           of the best non-pinned score are dropped, f in 0.25, 0.5, 0.75 (a trial, not shipped);
  arm A    the existing additive semantic channel (`Retriever.semantic`, attached through
           `AgentMemory.attach_semantic`) with SEMANTIC_FLOOR in 0.35 ... 0.75;
  arm B    an exploratory confirmation filter: a record whose only lexical evidence is a single
           concept (the weak and medium tiers) survives only if its cosine similarity to the task
           text is >= tau, tau in 0.3 ... 0.7; strong evidence (two or more concepts, scope, a whole
           identifier, pinned) is untouched.
The adapter embeds each active record's text once per world with BAAI/bge-small-en-v1.5
(normalised, cosine) and answers `search(query, k)` with the hit shape the retriever reads.
`--insample` also runs the in-sample scorecard and held-out abstention with each setting;
`--save` writes tests/eval/bench/runs/dev-semantic-arms.json; `--query-prefix` repeats the semantic
arms with the model's recommended retrieval instruction in front of the query.
The patches are applied to the Retriever class for the run only. sentence-transformers is imported
lazily, so importing this module without it works.
"""
import argparse
import contextlib
import dataclasses
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import judge, model
from .build import build_world
from .stats import wilson

MODEL_NAME = "BAAI/bge-small-en-v1.5"
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
LEVER6_FRACTIONS = (0.25, 0.5, 0.75)
ARM_A_FLOORS = (0.35, 0.45, 0.55, 0.65, 0.75)
ARM_B_TAUS = (0.3, 0.4, 0.5, 0.6, 0.7)
BASELINE_RELEVANT_MISSED = {"backfill-billing-night", "sunday-drop-late"}  # missed by the final retriever
SPLIT = "dev"


# -- the model and the adapter ---------------------------------------------------------------------

def snapshot_revision() -> str:
    """The cached snapshot directory name of MODEL_NAME (its commit hash), or 'unknown'."""
    home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    snaps = Path(os.environ.get("HF_HUB_CACHE") or Path(home) / "hub") / (
        "models--" + MODEL_NAME.replace("/", "--")) / "snapshots"
    names = sorted(p.name for p in snaps.iterdir()) if snaps.is_dir() else []
    return ",".join(names) or "unknown"


def load_model():
    """The sentence-transformers model, offline and on the CPU. Returns (model, seconds)."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    started = time.perf_counter()
    from sentence_transformers import SentenceTransformer
    encoder = SentenceTransformer(MODEL_NAME, device="cpu")
    return encoder, time.perf_counter() - started


class EmbeddingAdapter:
    """`search(query, k)` over the active records of one AgentMemory, backed by sentence-transformers.

    Hits are agent_memory.embeddings_integration.EmbeddedResult objects (node_ref, score, ...), the
    shape GraphEmbeddedMemory returns and the retriever reads. `similarity(query)` returns the cosine
    of every indexed record to the query (arm B uses it)."""

    def __init__(self, memory, encoder, query_prefix: str = ""):
        self.memory = memory
        self.encoder = encoder
        self.query_prefix = query_prefix
        self.refs: List[Tuple[str, Any]] = []
        self.texts: List[str] = []
        self.vectors = None
        self._cos: Dict[str, Any] = {}
        self.index_seconds = 0.0
        self.query_seconds: List[float] = []

    def index(self) -> int:
        from agent_memory.record import KINDS, MemoryRecord
        started = time.perf_counter()
        for kind in KINDS:
            for node in self.memory.graph.nodes(kind):
                rec = MemoryRecord.from_node(node)
                if rec.status == "active" and rec.text:
                    self.refs.append(rec.ref)
                    self.texts.append(rec.text)
        self.vectors = self.encoder.encode(self.texts, normalize_embeddings=True, convert_to_numpy=True,
                                           batch_size=32, show_progress_bar=False)
        self.index_seconds = time.perf_counter() - started
        return len(self.refs)

    def _scores(self, query: str):
        """Cosine to every record, cached per query text; the first call per query is timed."""
        hit = self._cos.get(query)
        if hit is None:
            started = time.perf_counter()
            vec = self.encoder.encode([self.query_prefix + query], normalize_embeddings=True,
                                      convert_to_numpy=True, show_progress_bar=False)[0]
            hit = self._cos[query] = self.vectors @ vec
            self.query_seconds.append(time.perf_counter() - started)
        return hit

    def similarity(self, query: str) -> Dict[Tuple[str, Any], float]:
        return dict(zip(self.refs, (float(s) for s in self._scores(query))))

    def search(self, query: str, k: int = 5, **_unused):
        from agent_memory.embeddings_integration import EmbeddedResult
        scores = self._scores(query)
        top = sorted(range(len(self.refs)), key=lambda i: -float(scores[i]))[:k]
        return [EmbeddedResult(node_ref=self.refs[i], memory_type=self.refs[i][0], content=self.texts[i],
                               score=float(scores[i]), properties={}) for i in top]

    def forget_queries(self) -> None:
        self._cos.clear()


# -- the patches (applied to the Retriever class for the duration of a run) --------------------------

class _State:
    fraction: Optional[float] = None     # lever 6
    tau: Optional[float] = None          # arm B
    adapter: Any = None
    query: str = ""


def _pinned(rec) -> bool:
    return rec.priority == "must" and rec.scope == ["*"]


@contextlib.contextmanager
def patched_retriever():
    from agent_memory.retrieval import retriever as module
    from agent_memory.retrieval.evidence import WEAK
    original_rank, original_classify = module.Retriever._rank, module.classify

    def rank(self, query, files, kinds=None, strict=False, extra_refs=(), cands=None,
             role=None, symbol_text=""):
        _State.adapter, _State.query = getattr(self, "_arm_adapter", None), query
        ranking = original_rank(self, query, files, kinds, strict, extra_refs, cands, role, symbol_text)
        if _State.fraction is None or strict or not ranking.refs:
            return ranking
        scored = {ref: ranking.item(ref) for ref in ranking.refs}
        loose = [it.score for it in scored.values() if not _pinned(it.record)]
        if not loose:
            return ranking
        cut = _State.fraction * max(loose)
        return module._Ranking([ref for ref in ranking.refs
                                if _pinned(scored[ref].record) or scored[ref].score >= cut], ranking.item)

    def classify(**args):
        tier = original_classify(**args)
        if _State.tau is None or _State.adapter is None or args.get("identifier") or tier == "strong":
            return tier
        # here: a single concept, medium (rare word) or weak. The caller is `passes(ref)` of _rank.
        caller = sys._getframe(1)
        if caller.f_code.co_name != "passes" or "ref" not in caller.f_locals:
            return tier
        if _State.adapter.similarity(_State.query).get(caller.f_locals["ref"], 0.0) >= _State.tau:
            return tier
        return WEAK     # unconfirmed: dropped by the floor (a pinned record bypasses it)

    module.Retriever._rank, module.classify = rank, classify
    try:
        yield
    finally:
        module.Retriever._rank, module.classify = original_rank, original_classify
        _State.fraction = _State.tau = _State.adapter = None


# -- running the dev split ---------------------------------------------------------------------------

@contextlib.contextmanager
def dev_worlds():
    tasks = model.load_tasks(SPLIT)
    with tempfile.TemporaryDirectory() as tmp:
        built = {}
        for name in model.WORLDS:
            if any(t.world == name for t in tasks):
                built[name] = build_world(model.load_world(name), Path(tmp) / name)
        try:
            yield tasks, built
        finally:
            for item in built.values():
                getattr(item.memory, "close", lambda: None)()


def run(tasks, built) -> Dict[str, Any]:
    results = []
    for t in tasks:
        b = built[t.world]
        results.append(judge.judge(t, b.service.recall(t.task, t.files, max_tokens=2000)["prompt"],
                                   b.key_of, b.world))
    s = judge.summarise(results)
    empty = [r for r in results if r.empty]
    full = [r for r in results if not r.empty]
    return dict(
        hits=s.relevant_hits, total=s.relevant_total, noise_tokens=s.noise_tokens_per_task,
        noise_records=s.noise_per_task, clean=s.clean, empty=s.empty_tasks, leaks=s.leaks,
        noise_tokens_empty=sum(r.noise_tokens for r in empty) / len(empty) if empty else 0.0,
        noise_tokens_nonempty=sum(r.noise_tokens for r in full) / len(full) if full else 0.0,
        missed=[[r.world, r.task, k] for r in results for k in r.missed])


def configure(built, adapters, mode: str) -> None:
    """mode: 'none' (the retriever as is), 'channel' (arm A: adapter attached), 'filter' (arm B)."""
    for name, b in built.items():
        retriever = b.memory._retriever
        retriever.semantic = adapters[name] if mode == "channel" else None
        retriever._arm_adapter = adapters[name] if mode == "filter" else None


def row(label: str, r: Dict[str, Any]) -> str:
    return (f"| {label} | {r['hits'] / r['total']:.4f} ({r['hits']}/{r['total']}) | {r['noise_records']:.2f} | "
            f"{r['noise_tokens']:.1f} | {r['noise_tokens_empty']:.1f} | {r['noise_tokens_nonempty']:.1f} | "
            f"{r['clean']}/{r['empty']} | {r['leaks']} |")


HEADER = ("| Setting | Dev recall | Noise records/task | Noise tokens/task | on empty tasks | on non-empty tasks "
          "| Clean-abstain | Leaks |\n|---|---|---|---|---|---|---|---|")


def extra_missed(r: Dict[str, Any]) -> List[str]:
    return sorted({m[2] for m in r["missed"]} - BASELINE_RELEVANT_MISSED)


def insample(encoder, floors: Sequence[float], taus: Sequence[float], query_prefix: str) -> List[Dict[str, Any]]:
    """The in-sample scorecard and held-out abstention with each setting (their own store).
    Call inside patched_retriever()."""
    from agent_memory.retrieval import retriever as module
    from agent_memory.service import MemoryService

    from ..eval_service import service_fns
    from ..harness import evaluate
    from ..heldout import HELDOUT
    from ..run import build as build_insample
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        memory = build_insample(Path(tmp))
        adapter = EmbeddingAdapter(memory, encoder, query_prefix)
        adapter.index()
        fns = service_fns(MemoryService(memory=memory))
        retriever = memory._retriever
        default_floor = module.SEMANTIC_FLOOR
        settings = [("none", None)] + [("channel", f) for f in floors] + [("filter", t) for t in taus]
        for mode, value in settings:
            retriever.semantic = adapter if mode == "channel" else None
            retriever._arm_adapter = adapter if mode == "filter" else None
            _State.tau = value if mode == "filter" else None
            if mode == "channel":
                module.SEMANTIC_FLOOR = value
            card = evaluate(*fns)
            held = evaluate(*fns, scenarios=HELDOUT)
            out.append(dict(mode=mode, value=value, hits=card.include_hits, total=card.include_total,
                            leaks=card.leaks, abstain=f"{card.abstain_hits}/{card.abstain_total}",
                            held=f"{held.abstain_hits}/{held.abstain_total}",
                            failures=list(card.failures)))
            module.SEMANTIC_FLOOR = default_floor
        getattr(memory, "close", lambda: None)()
    return out


def peak_mb() -> Optional[float]:
    try:
        import psutil
        info = psutil.Process().memory_info()
        return getattr(info, "peak_wset", info.rss) / 2 ** 20
    except Exception:
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
            return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.eval.bench.arms")
    parser.add_argument("--insample", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--query-prefix", action="store_true")
    args = parser.parse_args(argv)
    from agent_memory.retrieval import retriever as module

    out: List[str] = []
    saved: Dict[str, Any] = {"split": SPLIT, "model": MODEL_NAME, "revision": snapshot_revision(), "runs": {}}

    def record(group: str, label: str, result: Dict[str, Any]) -> None:
        saved["runs"].setdefault(group, {})[label] = result

    with dev_worlds() as (tasks, built), patched_retriever():
        out.append(f"split {SPLIT}: {len(tasks)} tasks, worlds " +
                   ", ".join(f"{n} ({len(b.world.records)} records)" for n, b in built.items()))
        none = {n: None for n in built}
        configure(built, none, "none")
        base = run(tasks, built)
        record("baseline", "final retriever", base)
        out += ["", "## Final retriever (no patch)", HEADER, row("final retriever", base)]

        out += ["", "## Lever 6: rank-relative cut", HEADER]
        for f in LEVER6_FRACTIONS:
            _State.fraction = f
            r = run(tasks, built)
            record("lever6", str(f), r)
            out.append(row(f"f = {f}", r))
            out.append(f"|  (extra relevant lost: {', '.join(extra_missed(r)) or 'none'}) | | | | | | | |")
        _State.fraction = None

        encoder, load_seconds = load_model()
        out += ["", f"model {MODEL_NAME}, snapshot {snapshot_revision()}, load {load_seconds:.2f} s (CPU, offline)"]
        saved["load_seconds"] = load_seconds
        variants = [("", "")] + ([("with query instruction, ", QUERY_INSTRUCTION)] if args.query_prefix else [])
        for tag, prefix in variants:
            adapters = {n: EmbeddingAdapter(b.memory, encoder, prefix) for n, b in built.items()}
            index = {n: (a.index(), a.index_seconds) for n, a in adapters.items()}
            out += ["", f"{tag}index: " + ", ".join(f"{n} {c} records {s:.2f} s" for n, (c, s) in index.items())]
            saved.setdefault("index", {})[tag or "plain"] = {n: list(v) for n, v in index.items()}

            out += ["", f"## Arm A, {tag}SEMANTIC_FLOOR grid (SEMANTIC_K = {module.SEMANTIC_K})", HEADER]
            configure(built, adapters, "channel")
            default_floor = module.SEMANTIC_FLOOR
            for floor in ARM_A_FLOORS:
                module.SEMANTIC_FLOOR = floor
                r = run(tasks, built)
                record(f"armA {tag}".strip(), str(floor), r)
                out.append(row(f"floor {floor}", r))
                out.append(f"|  (extra relevant lost: {', '.join(extra_missed(r)) or 'none'}) | | | | | | | |")
            module.SEMANTIC_FLOOR = default_floor

            out += ["", f"## Arm B, {tag}confirmation filter tau grid", HEADER]
            configure(built, adapters, "filter")
            for tau in ARM_B_TAUS:
                _State.tau = tau
                r = run(tasks, built)
                record(f"armB {tag}".strip(), str(tau), r)
                out.append(row(f"tau {tau}", r))
                out.append(f"|  (extra relevant lost: {', '.join(extra_missed(r)) or 'none'}) | | | | | | | |")
            _State.tau = None

            # cost: cold encode + search per task query (the text the retriever passes: the task)
            times = []
            for a in adapters.values():
                a.forget_queries()
            for t in tasks:
                started = time.perf_counter()
                adapters[t.world].search(t.task, k=module.SEMANTIC_K)
                times.append(time.perf_counter() - started)
                adapters[t.world].forget_queries()
            out.append(f"\n{tag}query encode+search (cold, one per task): median {statistics.median(times) * 1000:.1f} ms, "
                       f"p95 {sorted(times)[int(0.95 * len(times))] * 1000:.1f} ms, max {max(times) * 1000:.1f} ms")
            saved.setdefault("query_ms", {})[tag or "plain"] = dict(
                median=statistics.median(times) * 1000, max=max(times) * 1000)

            if args.insample:
                out += ["", f"## In-sample scorecard and held-out, {tag}with each setting (python -m tests.eval.run --service)",
                        "| Setting | In-sample recall | Leaks | Abstention | Held-out | Failures |", "|---|---|---|---|---|---|"]
                rows = insample(encoder, ARM_A_FLOORS, ARM_B_TAUS, prefix)
                saved.setdefault("insample", {})[tag or "plain"] = rows
                for r in rows:
                    name = {"none": "final retriever", "channel": f"arm A floor {r['value']}",
                            "filter": f"arm B tau {r['value']}"}[r["mode"]]
                    out.append(f"| {name} | {r['hits']}/{r['total']} | {r['leaks']} | {r['abstain']} | {r['held']} | "
                               f"{'; '.join(f[:60] for f in r['failures']) or 'none'} |")
        peak = peak_mb()
        out.append(f"\npeak process memory: {peak:.0f} MB" if peak else "\npeak process memory: not measured")
        saved["peak_mb"] = peak

    if args.save:
        path = model.BENCH / "runs" / "dev-semantic-arms.json"
        path.parent.mkdir(exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(saved, indent=1) + "\n")
        out.append(f"saved {path}")
    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
