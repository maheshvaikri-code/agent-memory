"""Ranks memories for a task: channels, fusion, modifiers, floor, de-duplication, budget."""
from __future__ import annotations

import heapq
import logging
import math
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

from ..anchors import STALENESS_FACTOR, HashCache, norm_path, staleness
from ..record import BACKGROUND, GUARDRAIL, GUIDANCE, KINDS, MemoryRecord, NodeRef
from .budget import TokenCounter
from .channels import graph_neighbours, scope_scores
from .lexical import BM25
from .render import HEADERS, render, render_item
from .tokenize import _WORD, compound_tokens, token_set, tokenize, tokens_for

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
INDEX_CACHE = 4          # BM25 indexes kept (recall and check use different candidate sets)
SESSION_KINDS = ("open_question", "blocker", "partial_work")
_NONE: FrozenSet[str] = frozenset()
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
    """1 for the best score; scores within 1e-12 of a rank's first score share it."""
    by_value, rank, last = {}, 0, None
    for score in sorted(set(scores.values()), reverse=True):
        if last is None or score < last - 1e-12:
            rank, last = rank + 1, score
        by_value[score] = rank
    return {ref: by_value[score] for ref, score in scores.items()}


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
    if half_life is None:
        return 1.0
    age = _age_days(rec.updated_at or rec.created_at)
    if age is None:
        return 1.0
    return max(RECENCY_FLOOR, 0.5 ** (age / half_life))


def _feedback(rec: MemoryRecord) -> float:
    return (1 + rec.helpful) / (1 + rec.helpful + 2 * rec.harmful)


def _camel_groups(text: str) -> List[set]:
    """Token sets of each camelCase word: its split parts plus the joined form."""
    groups = []
    for word in _WORD.findall(text or ""):
        group = set(tokenize(word)) | set(compound_tokens(word))
        if len(group) > 1:
            groups.append(group)
    return groups


def _concepts(matched: FrozenSet[str], groups: List[set]) -> int:
    """Matched query words, counting a camelCase word once ("MongoDB": mongo, db, mongodb)."""
    remaining, count = set(matched), 0
    while remaining:
        count += 1
        frontier = [remaining.pop()]
        while frontier:
            token = frontier.pop()
            for group in groups:
                if token in group:
                    linked = group & remaining
                    remaining -= linked
                    frontier.extend(linked)
    return count


def _prefix(tokens: FrozenSet[str], order: Dict[str, int]) -> Tuple[str, ...]:
    """The tokens that prefix filtering probes for a set: rarest first, |A| - ceil(t*|A|) + 1."""
    ordered = sorted(tokens, key=order.__getitem__)
    return tuple(ordered[:len(ordered) - math.ceil(DEDUP_JACCARD * len(ordered) - 1e-9) + 1])


class _Index:
    """BM25 over one candidate set, plus the de-duplication prefixes it implies."""

    def __init__(self, records: Tuple[MemoryRecord, ...], bm25: BM25):
        self.records = records
        self.bm25 = bm25
        self._order: Optional[Dict[str, int]] = None
        self._prefixes: Dict[str, Tuple[str, ...]] = {}

    def prefix(self, text: str) -> Tuple[str, ...]:
        hit = self._prefixes.get(text)
        if hit is None:
            if self._order is None:     # rarest first across the candidate corpus
                df = self.bm25.df
                self._order = {t: i for i, t in enumerate(sorted(df, key=lambda t: (df[t], t)))}
            hit = self._prefixes[text] = _prefix(token_set(text), self._order)
        return hit


class _Ranking:
    """Ranked, de-duplicated refs. Items are built on demand, so a recall over thousands
    of survivors only allocates the items its packing loop actually looks at."""

    def __init__(self, refs: List[NodeRef], build: Callable[[NodeRef], ScoredMemory]):
        self.refs = refs
        self.item = build


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
        # ref -> (copy of the node's properties, record). Decoding is the main per-recall cost at
        # thousands of records; a snapshot comparison catches every update_node, whoever
        # makes it, so no write path has to remember to invalidate anything.
        self._records: Dict[NodeRef, tuple] = {}
        self._indexes: List[_Index] = []    # most recent first

    # -- candidates --------------------------------------------------------

    def _candidates(self, kinds: Optional[Iterable[str]]) -> Dict[NodeRef, MemoryRecord]:
        out: Dict[NodeRef, MemoryRecord] = {}
        for kind in (kinds or KINDS):
            if kind not in KINDS:
                continue
            resolved = _RESOLVED_FIELD.get(kind)
            for node in self.graph.nodes(kind):
                rec = self._record(node)
                if rec.status != "active":
                    continue
                if resolved and rec.fields.get(resolved):
                    continue
                out[rec.ref] = rec
        if len(self._records) > self.graph.node_count():
            # some cached records belong to removed nodes
            self._records = {ref: hit for ref, hit in self._records.items()
                             if self.graph.has_node(*ref)}
        return out

    def _record(self, node) -> MemoryRecord:
        ref = (node.type, node.id)
        hit = self._records.get(ref)
        if hit is not None and hit[0] == node.properties:   # dict comparison, no allocation
            return hit[1]
        rec = MemoryRecord.from_node(node)
        self._records[ref] = (dict(node.properties), rec)
        return rec

    def _index(self, cands: Dict[NodeRef, MemoryRecord]) -> "_Index":
        """Index over `cands`, reused while the candidate records are the same objects."""
        records = tuple(cands.values())     # holds the records, so identity stays meaningful
        for i, index in enumerate(self._indexes):
            seen = index.records
            if len(seen) == len(records) and all(a is b for a, b in zip(seen, records)):
                if i:
                    self._indexes.insert(0, self._indexes.pop(i))
                return index
        index = _Index(records, BM25({ref: tokens_for(rec.text) + compound_tokens(rec.text)
                                      for ref, rec in cands.items()}))
        self._indexes = [index] + self._indexes[:INDEX_CACHE - 1]
        return index

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
        ranking = self._rank(query, files, kinds, strict, extra_refs, cands)
        return [ranking.item(ref) for ref in ranking.refs]

    def _rank(self, query: str, files: List[str], kinds: Optional[Iterable[str]] = None,
              strict: bool = False, extra_refs: Iterable[NodeRef] = (),
              cands: Optional[Dict[NodeRef, MemoryRecord]] = None) -> "_Ranking":
        cands = dict(cands) if cands is not None else self._candidates(kinds)
        for ref in extra_refs:   # promoted decisions when kinds is restricted
            if ref not in cands and self.graph.has_node(*ref):
                rec = self._record(self.graph.get_node(*ref))
                if rec.status == "active":
                    cands[ref] = rec
        if not cands:
            return _Ranking([], lambda ref: None)

        stems = " ".join(os.path.splitext(os.path.basename(f))[0] for f in files)
        query_text = f"{query} {stems}"
        query_tokens = tokenize(query_text) + list(compound_tokens(query_text))
        words = _camel_groups(query_text)
        index = self._index(cands)
        bm25 = index.bm25
        lexical, hits = bm25.match(query_tokens)     # scores; matched terms
        scope, promoted = scope_scores(self.graph, cands, files)
        semantic = self._semantic_scores(query, cands)

        fused: Dict[NodeRef, float] = {}
        for name, scores, quality in (("lexical", lexical, None), ("scope", scope, scope),
                                      ("semantic", semantic, None)):
            ranks = _dense_ranks(scores)
            for ref, rank in ranks.items():
                q = quality[ref] if quality is not None else 1.0
                fused[ref] = fused.get(ref, 0.0) + WEIGHTS[name] * q / (RRF_K + rank)

        def passes(ref: NodeRef) -> bool:
            matched = hits.get(ref, _NONE)
            in_scope = scope.get(ref, 0.0) >= SCOPE_FLOOR
            if strict:
                distinctive = any(bm25.df[t] <= DISTINCTIVE_DF for t in matched)
                count = _concepts(matched, words)
                return count >= 2 or (count == 1 and (in_scope or distinctive))
            return bool(matched) or in_scope or ref in semantic

        survivors = {ref: score for ref, score in fused.items() if passes(ref)}

        linked: Dict[NodeRef, NodeRef] = {}
        inherited_scores: Dict[NodeRef, float] = {}
        if not strict and survivors:
            seeds = dict(heapq.nlargest(GRAPH_SEEDS, survivors.items(), key=lambda kv: kv[1]))
            for ref, (inherited, seed) in graph_neighbours(self.graph, seeds, cands).items():
                if ref not in survivors:
                    survivors[ref] = inherited_scores[ref] = inherited
                    linked[ref] = seed

        cache = HashCache(self.repo_root)
        final: Dict[NodeRef, float] = {}
        states: Dict[NodeRef, str] = {}
        for ref, base in survivors.items():
            rec = cands[ref]
            state = staleness(self.graph, ref, cache)
            if strict and state == "orphaned":
                continue
            tier = GUARDRAIL if ref in promoted else rec.tier
            final[ref] = (base * rec.confidence * STALENESS_FACTOR[state] * _recency(rec)
                          * TIER_BOOST[tier] * _feedback(rec))
            states[ref] = state
        order = sorted(final, key=lambda ref: (-final[ref], str(cands[ref].id)))
        kept = self._dedup_texts([cands[ref].text for ref in order], index.prefix)

        def build(ref: NodeRef) -> ScoredMemory:
            rec = cands[ref]
            channels: Dict[str, float] = {}
            for name, scores in (("lexical", lexical), ("scope", scope), ("semantic", semantic)):
                if ref in scores:
                    channels[name] = round(scores[ref], 4)
            if ref in inherited_scores:
                channels["graph"] = round(inherited_scores[ref], 6)
            text = rec.text
            if promoted.get(ref):
                text = f"{text} (constraint: {promoted[ref]})"
            matched = hits.get(ref, _NONE)
            return ScoredMemory(rec, final[ref], channels, states[ref],
                                GUARDRAIL if ref in promoted else rec.tier, text,
                                self._why(rec, matched, scope.get(ref), ref in semantic,
                                          linked.get(ref)), matched)

        return _Ranking([order[i] for i in kept], build)

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
    def _dedup(items: List[ScoredMemory],
               prefix_of: Optional[Callable[[str], Tuple[str, ...]]] = None) -> List[ScoredMemory]:
        texts = [item.record.text for item in items]
        return [items[i] for i in Retriever._dedup_texts(texts, prefix_of)]

    @staticmethod
    def _dedup_texts(texts: List[str],
                     prefix_of: Optional[Callable[[str], Tuple[str, ...]]] = None) -> List[int]:
        """Positions kept, in order: a text whose token Jaccard with an earlier kept text
        is >= DEDUP_JACCARD is dropped.

        Prefix filtering (a standard set-similarity join): with tokens in one global
        rarest-first order, two sets at Jaccard >= t must share a token among the first
        |A| - ceil(t*|A|) + 1 of each. Only kept items sharing such a prefix token are
        compared, so thousands of survivors with common words stay cheap. `prefix_of`
        supplies the prefix from a cached corpus-wide order; without it the order is
        built from `texts`.
        """
        if prefix_of is None:
            freq = Counter(t for text in texts for t in token_set(text))
            order = {t: i for i, t in enumerate(sorted(freq, key=lambda t: (freq[t], t)))}
            prefix_of = lambda text: _prefix(token_set(text), order)
        kept: List[int] = []
        kept_tokens: List[FrozenSet[str]] = []
        index: Dict[str, Any] = {}    # token -> kept position, or a list of them
        for position, text in enumerate(texts):
            tokens = token_set(text)
            prefix = prefix_of(text)
            seen: set = set()
            duplicate = False
            for t in prefix:
                at = index.get(t, ())
                for k in ((at,) if isinstance(at, int) else at):
                    if k in seen:
                        continue
                    seen.add(k)
                    other = kept_tokens[k]
                    if len(tokens & other) / len(tokens | other) >= DEDUP_JACCARD:
                        duplicate = True
                        break
                if duplicate:
                    break
            if not duplicate:
                for t in prefix:
                    at = index.get(t)
                    if at is None:
                        index[t] = len(kept)
                    elif isinstance(at, int):
                        index[t] = [at, len(kept)]
                    else:
                        at.append(len(kept))
                kept.append(position)
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

    def check(self, approach: str, files: Optional[List[str]] = None,
              limit: int = 10) -> List[ScoredMemory]:
        """Prior negative experience relevant to a proposed approach.

        Guardrail kinds only, plus decisions that constrain a module holding
        one of `files`. Stricter floor than recall(); orphaned records are
        excluded. This reports relevance, not contradiction: deciding whether
        the approach actually conflicts is the caller's job.
        """
        files = [norm_path(f, self.repo_root) for f in (files or [])]
        guardrails = [kind for kind, spec in KINDS.items() if spec.tier == GUARDRAIL]
        _, promoted = scope_scores(self.graph, self._candidates(["decision"]), files)
        ranking = self._rank(approach or "", files, guardrails, strict=True, extra_refs=promoted)
        return [ranking.item(ref) for ref in ranking.refs[:limit]]

    def recall(self, task: str = "", files: Optional[List[str]] = None,
               symbols: Optional[List[str]] = None, kinds: Optional[List[str]] = None,
               max_tokens: int = 2000, compact: bool = False) -> RecallResult:
        kinds = kinds or None               # an empty list means "all kinds", like None
        files = [norm_path(f, self.repo_root) for f in (files or [])]
        query = " ".join([task or ""] + list(symbols or []))
        cands = self._candidates(kinds)     # read once per recall
        ranking = self._rank(query, files, kinds, cands=cands)
        ranked_refs = set(ranking.refs)
        # must/"*" records that also ranked keep their ranked item (and its identity)
        prebuilt = {ref: ranking.item(ref) for ref, rec in cands.items()
                    if rec.priority == "must" and rec.scope == ["*"] and ref in ranked_refs}
        pinned = self._pinned(cands, prebuilt) if kinds is None else []
        taken = ranked_refs | {item.record.ref for item in pinned}
        carried = self._open_session_items(cands, taken) if kinds is None else []

        budget = max_tokens - self.counter.count("\n".join(HEADERS.values()) + "\n")
        pinned_cap = int(max_tokens * PINNED_BUDGET_SHARE)
        selected: List[ScoredMemory] = []
        used = pinned_used = 0

        def pack(group: Iterable[ScoredMemory], cap: Optional[int] = None) -> None:
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
        pack(prebuilt[ref] if ref in prebuilt else ranking.item(ref)
             for ref in ranking.refs if ref not in packed)
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
