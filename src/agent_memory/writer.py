"""The single write path for memory records."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import anchors as anchors_mod
from .record import (
    KINDS, NATURAL_KEY, PENDING_TTL_DAYS, PRIORITIES, SESSION_KINDS, SOURCE_CONFIDENCE,
    STORED_PENDING, MemoryRecord, NodeRef, build_text, envelope_props, is_closed, make_id,
    normalize_text, pending_expired, utc_now,
)
from .retrieval.tokenize import tokens_for
from .storage.txn import transactional

SIMILAR_JACCARD = 0.6
REINFORCE_STEP = 0.1
CONFIDENCE_CAP = 0.95


@dataclass
class WriteResult:
    ref: NodeRef
    created: bool
    merged: bool = False
    similar: List[NodeRef] = field(default_factory=list)

    @property
    def id(self):
        return self.ref[1]


class MemoryWriter:
    def __init__(self, graph, repo_root: Optional[str] = None, agent: str = "unknown"):
        self.graph = graph
        self.repo_root = repo_root
        self.agent = agent
        self.session_id = ""
        self._by_text: Dict[Tuple[str, str], NodeRef] = {}
        self._indexed: set = set()
        self._remote_seen = 0

    # -- lookup ------------------------------------------------------------

    def find(self, memory_id) -> NodeRef:
        for kind in KINDS:
            if self.graph.has_node(kind, memory_id):
                return (kind, memory_id)
        raise KeyError(f"No memory with id {memory_id!r}")

    def _active_with_text(self, kind: str, norm: str) -> Optional[NodeRef]:
        """The active record of `kind` with this text that a write may merge into.

        An answered question or a resolved blocker is still active (it is history) but is
        never a merge target: asking the same question again is a new open question.
        """
        remote = getattr(self.graph, "remote_ops", 0)
        if remote != self._remote_seen:           # other processes changed the graph
            self._by_text.clear()
            self._indexed.clear()
            self._remote_seen = remote
        if kind not in self._indexed:
            for node in self.graph.nodes(kind):
                rec = MemoryRecord.from_node(node)
                if rec.status == "active" and not is_closed(kind, rec.fields):
                    self._by_text.setdefault((kind, normalize_text(rec.text)), rec.ref)
            self._indexed.add(kind)
        ref = self._by_text.get((kind, norm))
        if ref is None or not self.graph.has_node(*ref):
            return None
        rec = MemoryRecord.from_node(self.graph.get_node(*ref))
        if (rec.status != "active" or normalize_text(rec.text) != norm
                or is_closed(kind, rec.fields)):
            self._by_text.pop((kind, norm), None)
            return None
        return ref

    def named(self, kind: str, base_id: str, match: Dict[str, Any]) -> List[MemoryRecord]:
        """Records a store's name-built id can mean: the id itself or one the writer suffixed
        from it (base_<hash>[_n], written when base_id was taken), whose `match` fields equal
        the given values (whitespace-collapsed, case-insensitive)."""
        want = {k: normalize_text(str(v)) for k, v in match.items()}
        prefix = f"{base_id}_"
        out = []
        for node in self.graph.nodes(kind):
            nid = str(node.id)
            if nid != base_id and not nid.startswith(prefix):
                continue
            if all(normalize_text(str(node.properties.get(k) or "")) == v for k, v in want.items()):
                out.append(MemoryRecord.from_node(node))
        return out

    def resolve(self, kind: str, base_id: str, match: Dict[str, Any]) -> str:
        """The id a store means when it rebuilds `base_id` from a name or a text prefix.

        Several records can share a name-built id prefix (two questions with the same first
        30 characters; a module re-added with a new purpose). The record whose `match` fields
        equal the caller's wins, preferring active over inactive and open over answered or
        resolved. When no record matches exactly, `base_id` is returned unchanged (the old
        behaviour). Raises ValueError when several records match equally well.
        """
        if not self.graph.has_node(kind, base_id):
            return base_id          # a suffixed id is only ever written when base_id is taken
        named = self.named(kind, base_id, match)
        if not named:
            return base_id

        def rank(rec: MemoryRecord) -> Tuple[bool, bool]:
            return (rec.status != "active", is_closed(kind, rec.fields))

        best = min(rank(r) for r in named)
        group = [r for r in named if rank(r) == best]
        if len(group) > 1:
            raise ValueError(f"{len(group)} {kind} records match {match!r} "
                             f"({', '.join(str(r.id) for r in group)}); use the record id")
        return group[0].id

    # -- writes ------------------------------------------------------------

    @transactional
    def add(self, kind: str, fields: Dict[str, Any], *, node_id=None,
            anchors: Iterable = (), scope: Optional[List[str]] = None, evidence: str = "",
            priority: Optional[str] = None, confidence: Optional[float] = None,
            source: str = "explicit", status: str = "active",
            rejected_before: str = "") -> WriteResult:
        if kind not in KINDS:
            raise ValueError(f"Unknown memory kind {kind!r}. Valid kinds: {', '.join(sorted(KINDS))}")
        if priority is not None and priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}, got {priority!r}")
        if status not in ("active", "pending"):
            raise ValueError(f"status must be 'active' or 'pending', got {status!r}")
        pending = status == "pending"
        if pending and node_id is not None:
            raise ValueError("a pending record cannot be added under an explicit id: "
                             "that path replaces an active note")
        props = {k: ("" if v is None else v) for k, v in fields.items()}
        text = build_text(kind, props)
        norm = normalize_text(text)
        if not norm and node_id is None:
            raise ValueError(f"A {kind} memory has no text; check the field names for this kind")

        existing = self._active_with_text(kind, norm) if norm and not pending else None
        if existing is not None:
            if kind in SESSION_KINDS:
                # the open item now belongs to the session that recorded it again
                sid = str(props.get("session_id") or self.session_id or "")
                if sid and self.graph.get_node(*existing).properties.get("session_id") != sid:
                    self.graph.update_node(existing[0], existing[1], session_id=sid)
            self._reinforce(existing, evidence)
            self._anchor_all(existing, kind, props, anchors)
            return WriteResult(existing, created=False, merged=True)

        similar = self._similar(kind, text) if node_id is None and not pending else []

        new_id = node_id if node_id is not None else make_id(kind, text)
        replaces: List[NodeRef] = []
        if self.graph.has_node(kind, new_id):
            key = NATURAL_KEY.get(kind)
            if node_id is not None and key and str(props.get(key) or "").strip():
                # "add or update": the same key with a new text replaces the active record
                replaces = [r.ref for r in self.named(kind, node_id, {key: props[key]})
                            if r.status == "active"]
            base = f"{new_id}_{make_id(kind, text).rsplit('_', 1)[1]}" if node_id is not None else new_id
            new_id, n = base, 2
            while self.graph.has_node(kind, new_id):
                new_id, n = f"{base}_{n}", n + 1

        envelope = envelope_props(agent=self.agent, session_id=self.session_id, source=source,
                                  evidence=evidence, commit=anchors_mod.head_commit(self.repo_root),
                                  scope=scope, priority=priority, confidence=confidence,
                                  status=status)
        for key, value in envelope.items():
            props.setdefault(key, value)   # kind fields win, e.g. a decision's own status
        # only the writer marks a record as waiting for review: a pending add is always
        # pending whatever the fields say, and no other add can be
        props["review"] = envelope["review"]
        if pending:
            props.update(STORED_PENDING)
        # the label is the memory's own: a field of that name is dropped, and only a pending
        # add can carry one
        props.pop("rejected_before", None)
        if pending and rejected_before:
            props["rejected_before"] = rejected_before
        self.graph.add_node(kind, new_id, **props)
        ref = (kind, new_id)
        if norm and not pending:
            self._by_text[(kind, norm)] = ref
        self._anchor_all(ref, kind, props, anchors)
        for old in replaces:
            self._replace(old, ref, evidence)
        return WriteResult(ref, created=True, similar=similar)

    @transactional
    def supersede(self, old_ref: NodeRef, kind: str, fields: Dict[str, Any], **kw) -> WriteResult:
        self.graph.get_node(*old_ref)
        if kw.get("status") == "pending":
            raise ValueError("a pending record cannot supersede an active note")
        result = self.add(kind, fields, **kw)
        if result.ref == old_ref:
            return result
        self._set_status(old_ref, "superseded")
        if not self.graph.has_edge("SUPERSEDES", result.ref, old_ref):
            self.graph.add_edge("SUPERSEDES", result.ref, old_ref,
                                reason=kw.get("evidence", ""), date=utc_now())
        return result

    @transactional
    def retract(self, ref: NodeRef, reason: str = "") -> None:
        self._set_status(ref, "retracted")
        if reason:
            self._append_evidence(ref, f"retracted: {reason}")

    @transactional
    def feedback(self, ref: NodeRef, helpful: bool) -> None:
        rec = MemoryRecord.from_node(self.graph.get_node(*ref))
        if helpful:
            self.graph.update_node(ref[0], ref[1], helpful=rec.helpful + 1)
        else:
            self.graph.update_node(ref[0], ref[1], harmful=rec.harmful + 1)

    # -- lessons waiting for review -----------------------------------------

    def find_text(self, kind: str, fields: Dict[str, Any]) -> Optional[Tuple[NodeRef, str]]:
        """(ref, "active" | "pending") of a record of `kind` whose text equals the text these
        fields build, preferring an active one. An answered question or resolved blocker is
        history, not a match (the same rule as a merge)."""
        props = {k: ("" if v is None else v) for k, v in fields.items()}
        norm = normalize_text(build_text(kind, props))
        if not norm:
            return None
        pending_hit: Optional[Tuple[NodeRef, str]] = None
        for node in self.graph.nodes(kind):
            rec = MemoryRecord.from_node(node)
            if rec.status not in ("active", "pending") or normalize_text(rec.text) != norm:
                continue
            if rec.status == "active":
                if is_closed(kind, rec.fields):
                    continue
                return rec.ref, "active"
            pending_hit = pending_hit or (rec.ref, "pending")
        return pending_hit

    def find_retracted(self, kind: str, fields: Dict[str, Any]) -> Optional[str]:
        """"<id> on <date>" of the most recently retracted record of `kind` with the text these
        fields build, or None. A superseded or archived record is not a rejection."""
        props = {k: ("" if v is None else v) for k, v in fields.items()}
        norm = normalize_text(build_text(kind, props))
        if not norm:
            return None
        best: Optional[MemoryRecord] = None
        for node in self.graph.nodes(kind):
            rec = MemoryRecord.from_node(node)
            if rec.status == "retracted" and normalize_text(rec.text) == norm:
                if best is None or rec.updated_at >= best.updated_at:   # a tie: the later node
                    best = rec
        return None if best is None else f"{best.id} on {best.updated_at[:10]}"

    def pending(self) -> List[MemoryRecord]:
        """Every record waiting for review, oldest first."""
        out = [MemoryRecord.from_node(node) for kind in KINDS for node in self.graph.nodes(kind)]
        out = [rec for rec in out if rec.status == "pending"]
        out.sort(key=lambda rec: (rec.created_at, str(rec.id)))
        return out

    @transactional
    def accept(self, ref: NodeRef) -> None:
        """A person confirmed this pending record: it becomes an active, reviewed note."""
        rec = MemoryRecord.from_node(self.graph.get_node(*ref))
        if rec.status != "pending":
            raise ValueError(f"{ref[1]!r} is {rec.status}, not pending")
        hit = self.find_text(ref[0], rec.fields)
        if hit is not None and hit[1] == "active" and hit[0] != ref:
            raise ValueError(f"an active note with the same text already exists ({hit[0][1]}); "
                             "reject this lesson instead")
        self.graph.update_node(ref[0], ref[1], status="active", review="", source="reviewed",
                               confidence=SOURCE_CONFIDENCE["reviewed"], invalid_at="",
                               updated_at=utc_now())
        if not is_closed(ref[0], rec.fields):
            # index it, so the next active add of this text merges instead of duplicating
            self._by_text[(ref[0], normalize_text(rec.text))] = ref

    @transactional
    def expire_pending(self, now: Optional[datetime] = None, days: int = PENDING_TTL_DAYS) -> int:
        """Archive pending records older than `days`. Returns how many."""
        count = 0
        for rec in self.pending():
            if pending_expired(rec.created_at, now=now, days=days):
                self._set_status(rec.ref, "archived")
                count += 1
        return count

    # -- helpers -----------------------------------------------------------

    def _replace(self, old: NodeRef, new: NodeRef, evidence: str = "") -> None:
        """`new` updates `old` (same natural key): supersede it and take over its links, so
        constraints, fixes, attempts and anchors attached to the old record still apply.
        Anchors are re-stamped at the current file hash, since the new text is written now."""
        self._set_status(old, "superseded")
        if not self.graph.has_edge("SUPERSEDES", new, old):
            self.graph.add_edge("SUPERSEDES", new, old, reason=evidence, date=utc_now())
        commit = None
        for edge in list(self.graph.edges(source=old)):
            if edge.rel_type == "SUPERSEDES" or edge.target == new:
                continue
            if edge.rel_type == anchors_mod.EDGE:
                if not self.graph.has_edge(edge.rel_type, new, edge.target) and self.graph.has_node(*edge.target):
                    if commit is None:
                        commit = anchors_mod.head_commit(self.repo_root)
                    path = str(self.graph.get_node(*edge.target).properties.get("path") or "")
                    anchors_mod.add_anchor(self.graph, new, path, str(edge.properties.get("symbol") or ""),
                                           self.repo_root, commit)
            elif not self.graph.has_edge(edge.rel_type, new, edge.target):
                self.graph.add_edge(edge.rel_type, new, edge.target, **dict(edge.properties))
        for edge in list(self.graph.edges(target=old)):
            if edge.rel_type == "SUPERSEDES" or edge.source == new:
                continue
            if not self.graph.has_edge(edge.rel_type, edge.source, new):
                self.graph.add_edge(edge.rel_type, edge.source, new, **dict(edge.properties))

    def _set_status(self, ref: NodeRef, status: str) -> None:
        """Every status change goes through here. It clears a pending mark: an expired lesson
        is stored "archived" too, and must not still read as waiting for review."""
        now = utc_now()
        extra = {"review": ""} if self.graph.get_node(*ref).properties.get("review") else {}
        self.graph.update_node(ref[0], ref[1], status=status, invalid_at=now, updated_at=now,
                               **extra)

    def _append_evidence(self, ref: NodeRef, extra: str) -> None:
        rec = MemoryRecord.from_node(self.graph.get_node(*ref))
        if extra and extra not in rec.evidence:
            merged = f"{rec.evidence}; {extra}" if rec.evidence else extra
            self.graph.update_node(ref[0], ref[1], evidence=merged)

    def _reinforce(self, ref: NodeRef, evidence: str) -> None:
        rec = MemoryRecord.from_node(self.graph.get_node(*ref))
        self.graph.update_node(ref[0], ref[1],
                               confidence=min(CONFIDENCE_CAP, rec.confidence + REINFORCE_STEP),
                               updated_at=utc_now())
        self._append_evidence(ref, evidence)

    def _similar(self, kind: str, text: str) -> List[NodeRef]:
        mine = set(tokens_for(text))
        if not mine:
            return []
        out = []
        for node in self.graph.nodes(kind):
            rec = MemoryRecord.from_node(node)
            if rec.status != "active":
                continue
            theirs = set(tokens_for(rec.text))
            union = mine | theirs
            if union and len(mine & theirs) / len(union) >= SIMILAR_JACCARD:
                out.append(rec.ref)
        return out

    def _anchor_all(self, ref: NodeRef, kind: str, props: Dict[str, Any], anchors: Iterable) -> None:
        commit = anchors_mod.head_commit(self.repo_root)
        for item in anchors:
            path, symbol = (item, "") if isinstance(item, str) else (item[0], item[1])
            anchors_mod.add_anchor(self.graph, ref, path, symbol, self.repo_root, commit)
        field_name = KINDS[kind].scope_field
        value = props.get(field_name) if field_name else None
        if self.repo_root and value and "," not in str(value):
            rel, inside = anchors_mod.anchor_path(str(value), self.repo_root)
            if not inside:      # never auto-anchor a file outside the repo
                return
            try:
                is_file = (Path(self.repo_root) / rel).is_file()
            except (OSError, ValueError):
                is_file = False
            if is_file:
                anchors_mod.add_anchor(self.graph, ref, rel, "", self.repo_root, commit)
