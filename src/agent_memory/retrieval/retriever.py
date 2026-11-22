"""Ranks memories for a task: channels, fusion, modifiers, floor, de-duplication, budget."""
from __future__ import annotations

import copy
import heapq
import logging
import math
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

from ..anchors import STALENESS_FACTOR, HashCache, norm_path, staleness
from ..record import (BACKGROUND, GUARDRAIL, GUIDANCE, KINDS, RESOLVED_FIELD, SESSION_KINDS,
                      MemoryRecord, NodeRef)
from .budget import TokenCounter
from .channels import graph_neighbours, scope_scores
from .evidence import GENERIC_STEMS, SMALL_STORE, WEAK, classify
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
INDEX_CACHE = 4          # BM25 indexes kept, one per candidate-set role (recall, check, ...)
INDEX_REFRESH_MIN = 64   # changed records an index absorbs in place (or a tenth, if larger)
_NONE: FrozenSet[str] = frozenset()
_RESOLVED_FIELD = RESOLVED_FIELD
CARRY_DAYS = 14          # an unended session older than this is a crashed one: not carried
CARRY_MAX = 10
PREFIX_CACHE_MAX = 4096  # cached de-dup prefixes per index; cleared when full (long-lived processes)
ORDER_SLACK = 1024       # dead tokens tolerated in the token order before it is rebuilt


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


def _age_days(timestamp: str, now: Optional[datetime] = None) -> Optional[float]:
    """Days since `timestamp` at `now` (default: the current time)."""
    if not timestamp:
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now if now is not None else datetime.now(timezone.utc)
    return max(0.0, (now - when).total_seconds() / 86400)


def _recency(rec: MemoryRecord, now: Optional[datetime] = None) -> float:
    half_life = KINDS[rec.kind].half_life_days
    if half_life is None:
        return 1.0
    age = _age_days(rec.updated_at or rec.created_at, now)
    if age is None:
        return 1.0
    return max(RECENCY_FLOOR, 0.5 ** (age / half_life))


def _one_line(text: str) -> str:
    """Whitespace collapsed, as build_text does for record text, so a stored string can
    never start a line (or a fake section) of its own in the rendered prompt."""
    return " ".join(str(text).split())


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


_ATOMIC = (str, int, float, bool, type(None))


def _snapshot(props: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of node properties that an in-place change to a list value cannot reach."""
    return {k: v if isinstance(v, _ATOMIC) else copy.deepcopy(v) for k, v in props.items()}


def _types(props: Dict[str, Any]) -> Tuple[Tuple[str, type], ...]:
    """Types of the non-string values. Only those can equal a value of another type
    (1 == True == 1.0); a string never equals a non-string."""
    return tuple((k, type(v)) for k, v in props.items() if type(v) is not str)


def _detach(item: ScoredMemory) -> ScoredMemory:
    """Give a returned item its own record, so a caller who changes it cannot change
    the cached record that later recalls are built from."""
    rec = copy.copy(item.record)
    rec.fields = _snapshot(rec.fields)
    rec.scope = list(rec.scope)
    item.record = rec
    return item


def _doc(rec: MemoryRecord) -> Tuple[str, ...]:
    return tokens_for(rec.text) + compound_tokens(rec.text)


class _Index:
    """BM25 over one candidate set, plus the de-duplication prefixes it implies."""

    def __init__(self, cands: Dict[NodeRef, MemoryRecord]):
        self.records = tuple(cands.values())    # holds the records, so identity stays meaningful
        self.by_ref = dict(cands)
        self.bm25 = BM25({ref: _doc(rec) for ref, rec in cands.items()})
        self._order: Optional[Dict[str, int]] = None
        self._prefixes: Dict[str, Tuple[str, ...]] = {}
        self._next_rare = -1

    def same(self, records: Tuple[MemoryRecord, ...]) -> bool:
        seen = self.records
        return len(seen) == len(records) and all(a is b for a, b in zip(seen, records))

    def refresh(self, cands: Dict[NodeRef, MemoryRecord], limit: int) -> Optional[int]:
        """Bring the index up to `cands` in place when at most `limit` records differ.
        Returns the number of documents removed, or None when it would not refresh."""
        changed = {}
        for ref, rec in cands.items():
            if self.by_ref.get(ref) is not rec:
                changed[ref] = rec
                if len(changed) > limit:
                    return None
        gone = [ref for ref in self.by_ref if ref not in cands]
        if len(changed) + len(gone) > limit:
            return None
        remove = [ref for ref in changed if ref in self.by_ref] + gone
        self.bm25.update(remove, {ref: _doc(rec) for ref, rec in changed.items()})
        if self._order is not None:
            # Prefix filtering needs one fixed total order, not an exact one: existing
            # tokens keep their place (so cached prefixes stay valid), new ones go first.
            for rec in changed.values():
                for t in tokens_for(rec.text):
                    if t not in self._order:
                        self._order[t] = self._next_rare
                        self._next_rare -= 1
            if len(self._order) > len(self.bm25.df) + ORDER_SLACK:
                # tokens of long-gone records pile up in a long-lived process: rebuild lazily
                self._order = None
                self._prefixes.clear()
                self._next_rare = -1
        self.records = tuple(cands.values())
        self.by_ref = dict(cands)
        return len(remove)

    def prefix(self, text: str) -> Tuple[str, ...]:
        hit = self._prefixes.get(text)
        if hit is None:
            if self._order is None:     # rarest first across the candidate corpus
                df = self.bm25.df
                self._order = {t: i for i, t in enumerate(sorted(df, key=lambda t: (df[t], t)))}
            if len(self._prefixes) >= PREFIX_CACHE_MAX:
                self._prefixes.clear()
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
        # The writer's current session id; AgentMemory binds it. Its open items are carried.
        self.current_session: Callable[[], str] = lambda: ""
        self._semantic_warned = False
        self._sink_warned = False
        # ref -> (snapshot of the node's properties, their types, record). Decoding is the main
        # per-recall cost at thousands of records; comparing with the snapshot catches every
        # change to the node, whoever makes it, so no write path has to invalidate anything.
        # Cached records are shared between recalls and never handed out (see _detach).
        self._records: Dict[NodeRef, tuple] = {}
        # One index per candidate-set role (recall's kinds, check's guardrails, ...), least
        # recently used first. An index is only ever refreshed against its own role's
        # candidates, so alternating recall() and check() never converts one into the other.
        self._indexes: Dict[Any, _Index] = {}
        self.index_stats = {"builds": 0, "refreshes": 0, "removed": 0}

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
        props = node.properties
        hit = self._records.get(ref)
        # equal values of another type (1 -> True, 1 -> 1.0) compare equal, so check types too
        if (hit is not None and hit[0] == props
                and (not hit[1] or all(type(props[k]) is t for k, t in hit[1]))):
            return hit[2]
        rec = MemoryRecord.from_node(node)
        self._records[ref] = (_snapshot(props), _types(props), rec)
        return rec

    def _index(self, cands: Dict[NodeRef, MemoryRecord], role: Any) -> "_Index":
        """The index for `role` over `cands`: reused while the candidate records are the
        same objects, updated in place when only a few changed, rebuilt otherwise."""
        index = self._indexes.pop(role, None)
        if index is not None and not index.same(tuple(cands.values())):
            removed = index.refresh(cands, max(INDEX_REFRESH_MIN, len(cands) // 10))
            if removed is None:
                index = None
            else:
                self.index_stats["refreshes"] += 1
                self.index_stats["removed"] += removed
        if index is None:
            index = _Index(cands)
            self.index_stats["builds"] += 1
        self._indexes[role] = index           # most recently used last
        while len(self._indexes) > INDEX_CACHE:
            del self._indexes[next(iter(self._indexes))]
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
        return [_detach(ranking.item(ref)) for ref in ranking.refs]

    def _rank(self, query: str, files: List[str], kinds: Optional[Iterable[str]] = None,
              strict: bool = False, extra_refs: Iterable[NodeRef] = (),
              cands: Optional[Dict[NodeRef, MemoryRecord]] = None,
              role: Any = None, symbol_text: str = "") -> "_Ranking":
        if kinds is not None:
            kinds = list(kinds)         # a generator would be spent by the role below
        if role is None:
            role = tuple(kinds) if kinds else None
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
        identifiers = frozenset(compound_tokens(query_text))   # joined form of each camelCase word
        symbol_terms = frozenset(tokenize(symbol_text))        # terms the caller gave as symbols
        index = self._index(cands, role)
        bm25 = index.bm25
        lexical, hits = bm25.match(query_tokens)     # scores; matched terms
        scope, promoted = scope_scores(self.graph, cands, files)
        for ref, how in promoted.items():
            # a decision promoted by a CONSTRAINS edge is also matched by its constraint
            # text (`how`): it is part of the record's document for this query
            extra = tokens_for(how) + compound_tokens(how) if how else ()
            if extra and ref in cands:
                score, matched = bm25.match_extended(ref, extra, query_tokens)
                if matched:
                    lexical[ref], hits[ref] = score, matched
        semantic = self._semantic_scores(query, cands)

        fused: Dict[NodeRef, float] = {}
        for name, scores, quality in (("lexical", lexical, None), ("scope", scope, scope),
                                      ("semantic", semantic, None)):
            ranks = _dense_ranks(scores)
            for ref, rank in ranks.items():
                q = quality[ref] if quality is not None else 1.0
                fused[ref] = fused.get(ref, 0.0) + WEIGHTS[name] * q / (RRF_K + rank)

        tiers: Dict[NodeRef, str] = {}

        def passes(ref: NodeRef) -> bool:
            matched = hits.get(ref, _NONE)
            in_scope = scope.get(ref, 0.0) >= SCOPE_FLOOR
            if strict:
                distinctive = any(bm25.df[t] <= DISTINCTIVE_DF for t in matched)
                count = _concepts(matched, words)
                return count >= 2 or (count == 1 and (in_scope or distinctive))
            if not (matched or in_scope or ref in semantic):
                return False
            informative = matched - GENERIC_STEMS
            rec = cands.get(ref)
            # generic words count toward a record that also has an informative concept; a record
            # matched only by generic words is weak
            args: Dict[str, Any] = dict(
                in_scope=in_scope, semantic=ref in semantic,
                concepts=_concepts(matched, words) if informative else 0,
                rarest_df=min((bm25.df[t] for t in informative), default=None),
                n=bm25.n, distinctive_df=DISTINCTIVE_DF)
            tier = classify(**args)
            if tier == WEAK and informative and (
                    not identifiers.isdisjoint(informative)
                    or (rec is not None and symbol_terms
                        and not (informative & symbol_terms).isdisjoint(compound_tokens(rec.text)))):
                # a whole identifier is evidence however common it is in the store: a camelCase
                # word the task writes, or a symbol / context identifier (the service lower-cases
                # those) that the record spells as one compound token. A plain task word never
                # qualifies by how a record happens to spell it.
                tier = classify(identifier=True, **args)
            tiers[ref] = tier
            pinned = (rec is not None and rec.priority == "must"
                      and rec.scope == ["*"])     # always shown: keep its ranking data
            return tiers[ref] != WEAK or bm25.n < SMALL_STORE or pinned

        survivors = {ref: score for ref, score in fused.items() if passes(ref)}

        linked: Dict[NodeRef, NodeRef] = {}
        inherited_scores: Dict[NodeRef, float] = {}
        pool = {ref: v for ref, v in survivors.items() if tiers.get(ref) != WEAK}
        if not strict and pool:
            # the top GRAPH_SEEDS of the strong and medium survivors, ties broken by ref so the
            # result never depends on dict order; a weak survivor (a small store, or a pinned
            # record that matched weakly) does not pull its neighbours in
            floor = heapq.nlargest(GRAPH_SEEDS, pool.values())[-1]
            seeds = dict(sorted(((ref, v) for ref, v in pool.items() if v >= floor),
                                key=lambda kv: (-kv[1], str(kv[0])))[:GRAPH_SEEDS])
            for ref, (inherited, seed) in graph_neighbours(self.graph, seeds, cands).items():
                if ref not in survivors:
                    survivors[ref] = inherited_scores[ref] = inherited
                    linked[ref] = seed

        cache = HashCache(self.repo_root)
        final: Dict[NodeRef, float] = {}
        states: Dict[NodeRef, str] = {}
        # one clock reading for the whole ranking: read per record, exactly tied notes would
        # differ by the time between reads and their order would follow evaluation order
        now = datetime.now(timezone.utc)
        for ref, base in survivors.items():
            rec = cands[ref]
            state = staleness(self.graph, ref, cache)
            if strict and state == "orphaned":
                continue
            tier = GUARDRAIL if ref in promoted else rec.tier
            final[ref] = (base * rec.confidence * STALENESS_FACTOR[state] * _recency(rec, now)
                          * TIER_BOOST[tier] * _feedback(rec))
            states[ref] = state
        order = sorted(final, key=lambda ref: (-final[ref], str(cands[ref].id), ref[0]))
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
                text = f"{text} (constraint: {_one_line(promoted[ref])})"
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
            parts.append(f"{_one_line(rec.agent)}, {rec.created_at[:10]}")
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
        out.sort(key=lambda s: (-s.score, str(s.record.id), s.record.kind))
        return out

    def _carried_session(self) -> Optional[str]:
        """The session whose open items are carried: the writer's current session unless it
        has ended, else the most recently started unended session if it started within
        CARRY_DAYS. A session a crashed agent never ended stops being carried after that."""
        current = self.current_session() or ""
        if current:
            node = (self.graph.get_node("session", current)
                    if self.graph.has_node("session", current) else None)
            if node is None or not node.properties.get("ended"):
                return current
        latest, started = None, ""
        for node in self.graph.nodes("session"):
            if not node.properties.get("ended") and str(node.properties.get("started") or "") >= started:
                latest, started = node.id, str(node.properties.get("started") or "")
        age = _age_days(started)
        if latest is None or age is None or age > CARRY_DAYS:
            return None
        return latest

    def carried(self) -> List[ScoredMemory]:
        """The open session items recall() would carry (see _carried_session), as detached copies."""
        return [_detach(item) for item in self._open_session_items(self._candidates(None), set())]

    def _open_session_items(self, cands: Dict[NodeRef, MemoryRecord], taken: set) -> List[ScoredMemory]:
        session = self._carried_session()
        if session is None:
            return []
        recs = [rec for ref, rec in cands.items()
                # session kinds carry their session in the envelope's session_id
                if ref[0] in SESSION_KINDS and rec.session_id == session and ref not in taken]
        recs.sort(key=lambda r: (r.updated_at or r.created_at, str(r.id)), reverse=True)
        return [ScoredMemory(rec, 0.0, {"session": 1.0}, "unknown", BACKGROUND,
                             rec.text, "open from the current session")
                for rec in recs[:CARRY_MAX]]

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
        ranking = self._rank(approach or "", files, guardrails, strict=True, extra_refs=promoted,
                             role="check")
        return [_detach(ranking.item(ref)) for ref in ranking.refs[:limit]]

    def recall(self, task: str = "", files: Optional[List[str]] = None,
               symbols: Optional[List[str]] = None, kinds: Optional[List[str]] = None,
               max_tokens: int = 2000, compact: bool = False,
               always_on: Optional[bool] = None) -> RecallResult:
        """`always_on` adds the pinned (must/"*") and carried session items; by default
        only when `kinds` is not restricted. They are drawn from every kind."""
        kinds = list(kinds) if kinds is not None else None
        kinds = kinds or None               # an empty list means "all kinds", like None
        files = [norm_path(f, self.repo_root) for f in (files or [])]
        query = " ".join([task or ""] + list(symbols or []))
        cands = self._candidates(kinds)     # read once per recall
        ranking = self._rank(query, files, kinds, cands=cands,
                             symbol_text=" ".join(symbols or []))
        ranked_refs = set(ranking.refs)
        if always_on is None:
            always_on = kinds is None
        every = cands if kinds is None or not always_on else self._candidates(None)
        # must/"*" records that also ranked keep their ranked item (and its identity)
        prebuilt = {ref: ranking.item(ref) for ref, rec in cands.items()
                    if rec.priority == "must" and rec.scope == ["*"] and ref in ranked_refs}
        pinned = self._pinned(every, prebuilt) if always_on else []
        taken = ranked_refs | {item.record.ref for item in pinned}
        carried = self._open_session_items(every, taken) if always_on else []

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
        for item in result.items:
            _detach(item)
        if self.sink is not None:
            try:
                self.sink(RecallEvent(result.task, files, [i.record.id for i in result.items]))
            except Exception as exc:  # telemetry must never fail a recall
                if not self._sink_warned:
                    log.warning("recall sink failed and was skipped: %s", exc)
                    self._sink_warned = True
        return result
