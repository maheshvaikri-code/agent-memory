"""Ranks memories for a task: channels, fusion, modifiers, floor, de-duplication, budget."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional

from ..anchors import STALENESS_FACTOR, HashCache, norm_path, staleness
from ..record import BACKGROUND, GUARDRAIL, GUIDANCE, KINDS, MemoryRecord, NodeRef
from .budget import TokenCounter
from .channels import graph_neighbours, scope_scores
from .lexical import BM25
from .render import HEADERS, render, render_item
from .tokenize import tokenize, tokens_for

log = logging.getLogger(__name__)

# Tuned against tests/eval. Change here only.
RRF_K = 60
WEIGHTS = {"lexical": 1.0, "scope": 1.0, "semantic": 1.0}
TIER_BOOST = {GUARDRAIL: 1.3, GUIDANCE: 1.0, BACKGROUND: 0.8}
RECENCY_FLOOR = 0.3
SCOPE_FLOOR = 0.6
SEMANTIC_FLOOR = 0.35
SEMANTIC_K = 20
DEDUP_JACCARD = 0.8
PINNED_BUDGET_SHARE = 0.15
GRAPH_SEEDS = 10
DISTINCTIVE_DF = 2
SESSION_KINDS = ("open_question", "blocker", "partial_work")
_RESOLVED_FIELD = {"open_question": "answered_in", "blocker": "resolved_in"}


@dataclass
class ScoredMemory:
    record: MemoryRecord
    score: float
    channels: Dict[str, float]
    staleness: str
    tier: str
    text: str
    why: str = ""
    matched: FrozenSet[str] = frozenset()
    pinned: bool = False

    def to_dict(self) -> Dict[str, Any]:
        r = self.record
        return {"id": r.id, "kind": r.kind, "text": self.text, "score": round(self.score, 6),
                "why": self.why, "staleness": self.staleness, "tier": self.tier,
                "agent": r.agent, "created_at": r.created_at, "evidence": r.evidence,
                "confidence": r.confidence, "pinned": self.pinned, "channels": self.channels}


@dataclass
class RecallResult:
    task: str
    files: List[str]
    items: List[ScoredMemory] = field(default_factory=list)
    tokens: int = 0
    compact: bool = False

    def to_prompt(self) -> str:
        return render(self.items, self.compact)

    def to_dict(self) -> Dict[str, Any]:
        return {"task": self.task, "files": self.files, "tokens": self.tokens,
                "items": [i.to_dict() for i in self.items]}


@dataclass
class RecallEvent:
    task: str
    files: List[str]
    ids: List[Any]


def _dense_ranks(scores: Dict[NodeRef, float]) -> Dict[NodeRef, int]:
    ranks, rank, last = {}, 0, None
    for ref, score in sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        if last is None or score < last - 1e-12:
            rank, last = rank + 1, score
        ranks[ref] = rank
    return ranks


def _age_days(timestamp: str) -> Optional[float]:
    if not timestamp:
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400)


def _recency(rec: MemoryRecord) -> float:
    half_life = KINDS[rec.kind].half_life_days
    age = _age_days(rec.updated_at or rec.created_at)
    if half_life is None or age is None:
        return 1.0
    return max(RECENCY_FLOOR, 0.5 ** (age / half_life))


def _feedback(rec: MemoryRecord) -> float:
    return (1 + rec.helpful) / (1 + rec.helpful + 2 * rec.harmful)


class Retriever:
    def __init__(self, graph, repo_root: Optional[str] = None,
                 counter: Optional[TokenCounter] = None, semantic=None,
                 sink: Optional[Callable[[RecallEvent], None]] = None):
        self.graph = graph
        self.repo_root = repo_root
        self.counter = counter or TokenCounter.heuristic()
        self.semantic = semantic
        self.sink = sink
        self._semantic_warned = False
        self._sink_warned = False

    # -- candidates --------------------------------------------------------

    def _candidates(self, kinds: Optional[Iterable[str]]) -> Dict[NodeRef, MemoryRecord]:
        out: Dict[NodeRef, MemoryRecord] = {}
        for kind in (kinds or KINDS):
            if kind not in KINDS:
                continue
            resolved = _RESOLVED_FIELD.get(kind)
            for node in self.graph.nodes(kind):
                rec = MemoryRecord.from_node(node)
                if rec.status != "active":
                    continue
                if resolved and rec.fields.get(resolved):
                    continue
                out[rec.ref] = rec
        return out

    def _semantic_scores(self, query: str, cands) -> Dict[NodeRef, float]:
        if self.semantic is None or not query.strip():
            return {}
        try:
            hits = self.semantic.search(query, k=SEMANTIC_K)
            return {tuple(h.node_ref): float(h.score) for h in hits
                    if tuple(h.node_ref) in cands and float(h.score) >= SEMANTIC_FLOOR}
        except Exception as exc:  # the channel is optional; never fail a recall on it
            if not self._semantic_warned:
                log.warning("semantic channel failed and was skipped: %s", exc)
                self._semantic_warned = True
            return {}

    # -- ranking -----------------------------------------------------------

    def rank(self, query: str, files: List[str], kinds: Optional[Iterable[str]] = None,
             strict: bool = False, extra_refs: Iterable[NodeRef] = (),
             cands: Optional[Dict[NodeRef, MemoryRecord]] = None) -> List[ScoredMemory]:
        """Ranked survivors of the relevance floor. `strict` applies the guardrail-check floor."""
        cands = dict(cands) if cands is not None else self._candidates(kinds)
        for ref in extra_refs:   # promoted decisions when kinds is restricted
            if ref not in cands and self.graph.has_node(*ref):
                rec = MemoryRecord.from_node(self.graph.get_node(*ref))
                if rec.status == "active":
                    cands[ref] = rec
        if not cands:
            return []

        stems = " ".join(os.path.splitext(os.path.basename(f))[0] for f in files)
        query_tokens = tokenize(f"{query} {stems}")
        bm25 = BM25({ref: tokens_for(rec.text) for ref, rec in cands.items()})
        hits = {h.key: h for h in bm25.search(query_tokens)}
        lexical = {ref: h.score for ref, h in hits.items()}
        scope, promoted = scope_scores(self.graph, cands, files)
        semantic = self._semantic_scores(query, cands)

        fused: Dict[NodeRef, float] = {}
        channels: Dict[NodeRef, Dict[str, float]] = {}
        for name, scores, quality in (("lexical", lexical, None), ("scope", scope, scope),
                                      ("semantic", semantic, None)):
            ranks = _dense_ranks(scores)
            for ref, rank in ranks.items():
                q = quality[ref] if quality is not None else 1.0
                fused[ref] = fused.get(ref, 0.0) + WEIGHTS[name] * q / (RRF_K + rank)
                channels.setdefault(ref, {})[name] = round(scores[ref], 4)

        def passes(ref: NodeRef) -> bool:
            matched = hits[ref].matched if ref in hits else frozenset()
            in_scope = scope.get(ref, 0.0) >= SCOPE_FLOOR
            if strict:
                distinctive = any(bm25.df[t] <= DISTINCTIVE_DF for t in matched)
                return len(matched) >= 2 or (len(matched) == 1 and (in_scope or distinctive))
            return bool(matched) or in_scope or ref in semantic

        survivors = {ref: score for ref, score in fused.items() if passes(ref)}

        linked: Dict[NodeRef, NodeRef] = {}
        if not strict and survivors:
            seeds = dict(sorted(survivors.items(), key=lambda kv: -kv[1])[:GRAPH_SEEDS])
            for ref, (inherited, seed) in graph_neighbours(self.graph, seeds, cands).items():
                if ref not in survivors:
                    survivors[ref] = inherited
                    channels.setdefault(ref, {})["graph"] = round(inherited, 6)
                    linked[ref] = seed

        cache = HashCache(self.repo_root)
        out: List[ScoredMemory] = []
        for ref, base in survivors.items():
            rec = cands[ref]
            state = staleness(self.graph, ref, cache)
            if strict and state == "orphaned":
                continue
            tier = GUARDRAIL if ref in promoted else rec.tier
            score = (base * rec.confidence * STALENESS_FACTOR[state] * _recency(rec)
                     * TIER_BOOST[tier] * _feedback(rec))
            text = rec.text
            if promoted.get(ref):
                text = f"{text} (constraint: {promoted[ref]})"
            matched = hits[ref].matched if ref in hits else frozenset()
            out.append(ScoredMemory(rec, score, channels.get(ref, {}), state, tier, text,
                                    self._why(rec, matched, scope.get(ref), ref in semantic,
                                              linked.get(ref)), matched))
        out.sort(key=lambda s: (-s.score, str(s.record.id)))
        return self._dedup(out)

    @staticmethod
    def _why(rec: MemoryRecord, matched, scope_score, semantic: bool, seed) -> str:
        parts = []
        if matched:
            parts.append("matched: " + ", ".join(sorted(matched)))
        if scope_score:
            parts.append("applies to this file" if scope_score >= 1.0 else "nearby file")
        if semantic:
            parts.append("semantically similar")
        if seed is not None:
            parts.append(f"linked to {seed[1]}")
        if rec.agent and rec.agent != "unknown":
            parts.append(f"{rec.agent}, {rec.created_at[:10]}")
        return " · ".join(parts)

    @staticmethod
    def _dedup(items: List[ScoredMemory]) -> List[ScoredMemory]:
        kept: List[ScoredMemory] = []
        kept_tokens: List[set] = []
        for item in items:
            tokens = set(tokens_for(item.record.text))
            duplicate = False
            for other in kept_tokens:
                union = tokens | other
                if union and len(tokens & other) / len(union) >= DEDUP_JACCARD:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(item)
                kept_tokens.append(tokens)
        return kept

    # -- always-on sets ----------------------------------------------------

    @staticmethod
    def _pinned(cands: Dict[NodeRef, MemoryRecord],
                ranked: Dict[NodeRef, ScoredMemory]) -> List[ScoredMemory]:
        """Every must/'*' record. One that also matched the query keeps its ranked data."""
        out = []
        for ref, rec in cands.items():
            if rec.priority != "must" or rec.scope != ["*"]:
                continue
            item = ranked.get(ref)
            if item is not None:
                item.pinned = True
            else:
                item = ScoredMemory(rec, rec.confidence, {"pinned": 1.0}, "unknown",
                                    rec.tier, rec.text, "always applies", pinned=True)
            out.append(item)
        out.sort(key=lambda s: (-s.score, str(s.record.id)))
        return out

    def _open_session_items(self, cands: Dict[NodeRef, MemoryRecord], taken: set) -> List[ScoredMemory]:
        latest, started = None, ""
        for node in self.graph.nodes("session"):
            if not node.properties.get("ended") and str(node.properties.get("started") or "") >= started:
                latest, started = node.id, str(node.properties.get("started") or "")
        if latest is None:
            return []
        out = []
        for ref, rec in cands.items():
            # session kinds carry their session in the envelope's session_id
            if ref[0] in SESSION_KINDS and rec.session_id == latest and ref not in taken:
                out.append(ScoredMemory(rec, 0.0, {"session": 1.0}, "unknown", BACKGROUND,
                                        rec.text, "open from the current session"))
        return out

    # -- public ------------------------------------------------------------

    def recall(self, task: str = "", files: Optional[List[str]] = None,
               symbols: Optional[List[str]] = None, kinds: Optional[List[str]] = None,
               max_tokens: int = 2000, compact: bool = False) -> RecallResult:
        kinds = kinds or None               # an empty list means "all kinds", like None
        files = [norm_path(f, self.repo_root) for f in (files or [])]
        query = " ".join([task or ""] + list(symbols or []))
        cands = self._candidates(kinds)     # read once per recall
        ranked = self.rank(query, files, kinds, cands=cands)
        pinned = self._pinned(cands, {item.record.ref: item for item in ranked}) if kinds is None else []
        taken = {item.record.ref for item in ranked} | {item.record.ref for item in pinned}
        carried = self._open_session_items(cands, taken) if kinds is None else []

        budget = max_tokens - self.counter.count("\n".join(HEADERS.values()) + "\n")
        pinned_cap = int(max_tokens * PINNED_BUDGET_SHARE)
        selected: List[ScoredMemory] = []
        used = pinned_used = 0

        def pack(group: List[ScoredMemory], cap: Optional[int] = None) -> None:
            nonlocal used, pinned_used
            for item in group:
                cost = self.counter.count(render_item(item, compact) + "\n")
                if used + cost > budget:
                    continue          # skip, keep packing smaller items
                if cap is not None and pinned_used + cost > cap:
                    continue
                selected.append(item)
                used += cost
                if cap is not None:
                    pinned_used += cost

        pack(pinned, pinned_cap)
        # A pinned record that also matched the query but did not fit the pinned share
        # competes as an ordinary ranked item.
        packed = {item.record.ref for item in selected}
        for item in pinned:
            if item.record.ref not in packed:
                item.pinned = False
        pack([item for item in ranked if item.record.ref not in packed])
        pack(carried)

        result = RecallResult(task or "", files, selected, 0, compact)
        while result.items and self.counter.count(result.to_prompt()) > max_tokens:
            result.items.pop()
        result.tokens = self.counter.count(result.to_prompt())
        if self.sink is not None:
            try:
                self.sink(RecallEvent(result.task, files, [i.record.id for i in result.items]))
            except Exception as exc:  # telemetry must never fail a recall
                if not self._sink_warned:
                    log.warning("recall sink failed and was skipped: %s", exc)
                    self._sink_warned = True
        return result
