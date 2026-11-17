"""
Record model: which node types are memories, the envelope every memory
carries, and the one place that converts between node properties and
MemoryRecord.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

NodeRef = Tuple[str, Any]

GUARDRAIL, GUIDANCE, BACKGROUND = "guardrail", "guidance", "background"
TIER_ORDER = (GUARDRAIL, GUIDANCE, BACKGROUND)

PRIORITIES = ("must", "should", "info")
STATUSES = ("active", "superseded", "retracted", "archived")
SOURCE_CONFIDENCE = {"explicit": 0.7, "extracted": 0.5, "bootstrap": 0.5,
                     "imported": 0.6, "legacy": 0.6}
ENVELOPE_FIELDS = ("created_at", "updated_at", "agent", "session_id", "source",
                   "evidence", "commit", "scope", "priority", "confidence",
                   "status", "invalid_at", "helpful", "harmful")

# DecisionStore already writes a `status` with its own vocabulary.
_STATUS_ALIASES = {"accepted": "active", "proposed": "active", "deprecated": "archived"}
_LEGACY_TIME_FIELDS = ("added", "date", "started", "when", "discovered", "identified")


@dataclass(frozen=True)
class KindSpec:
    tier: str
    template: str
    half_life_days: Optional[int] = None
    scope_field: Optional[str] = None


KINDS: Dict[str, KindSpec] = {
    # guardrails: prior negative experience and hard limits
    "alternative": KindSpec(GUARDRAIL, "Rejected: {option} — {rejected_because}"),
    "bug_pattern": KindSpec(GUARDRAIL, "{name}: {signature}", scope_field="modules_affected"),
    "refactoring_attempt": KindSpec(GUARDRAIL, "Refactor attempt ({outcome}): {approach} — {reason_failed}"),
    "quirk": KindSpec(GUARDRAIL, "{description}"),
    "hot_path": KindSpec(GUARDRAIL, "Hot path {name} ({frequency})"),
    "performance_constraint": KindSpec(GUARDRAIL, "{operation}: {constraint} — {reason}"),
    # guidance: how things are done here
    "convention": KindSpec(GUIDANCE, "[{category}] {rule}", 365, "applies_to"),
    "idiom": KindSpec(GUIDANCE, "{pattern}: prefer {preferred_form}; avoid {avoid}", 365),
    "project_pattern": KindSpec(GUIDANCE, "{name}: {template} — {usage_context}", 365),
    "decision": KindSpec(GUIDANCE, "{title} — {context}", 365),
    "fix_pattern": KindSpec(GUIDANCE, "Fix: {fix_template} — {caveats}", 365),
    "workaround": KindSpec(GUIDANCE, "Workaround: {solution}{description}", 365),
    "test_convention": KindSpec(GUIDANCE, "[test:{category}] {pattern}", 365),
    "required_test_case": KindSpec(GUIDANCE, "Tests for {for_pattern} must cover: {cases_required}", 365),
    "template": KindSpec(GUIDANCE, "Template {name} ({category})", 365),
    "snippet": KindSpec(GUIDANCE, "{name}: {code} — {usage_context}", 365),
    # background: useful when relevant
    "module": KindSpec(BACKGROUND, "{path}: {purpose}", 180, "path"),
    "design_pattern": KindSpec(BACKGROUND, "{name} — {why_chosen}", 180, "where_used"),
    "tech_debt": KindSpec(BACKGROUND, "{location}: {description}", 180, "location"),
    "bug": KindSpec(BACKGROUND, "{title} — {root_cause}", 180, "module"),
    "expertise_area": KindSpec(BACKGROUND, "{domain} ({depth})", 180, "modules"),
    "session_decision": KindSpec(BACKGROUND, "{decision} — {rationale}", 30),
    "open_question": KindSpec(BACKGROUND, "{question}", 30),
    "blocker": KindSpec(BACKGROUND, "{description}", 30),
    "partial_work": KindSpec(BACKGROUND, "{work_type} at {location}: {state}", 30, "location"),
}


class _Blank(dict):
    def __missing__(self, key):
        return ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_text(kind: str, props: Mapping[str, Any]) -> str:
    """The one-statement form of a memory, built from its kind's fields."""
    values = _Blank({k: ("" if v is None else str(v)) for k, v in props.items()})
    text = KINDS[kind].template.format_map(values)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" :—-;")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def make_id(kind: str, text: str) -> str:
    norm = normalize_text(text)
    slug = re.sub(r"[^a-z0-9]+", "_", norm).strip("_")[:24].strip("_") or "x"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:6]
    return f"{kind}_{slug}_{digest}"


def envelope_props(*, agent: str = "unknown", session_id: str = "", source: str = "explicit",
                   evidence: str = "", commit: str = "", scope: Optional[List[str]] = None,
                   priority: Optional[str] = None, confidence: Optional[float] = None) -> Dict[str, Any]:
    """Envelope as node properties. Unset scope and priority are stored empty
    so the kind's derived defaults apply on read."""
    now = utc_now()
    return {
        "created_at": now, "updated_at": now, "agent": agent, "session_id": session_id,
        "source": source, "evidence": evidence, "commit": commit,
        "scope": json.dumps(list(scope)) if scope else "",
        "priority": priority or "",
        "confidence": float(confidence if confidence is not None else SOURCE_CONFIDENCE.get(source, 0.6)),
        "status": "active", "invalid_at": "", "helpful": 0, "harmful": 0,
    }


def _derived_scope(spec: KindSpec, props: Mapping[str, Any]) -> List[str]:
    raw = props.get(spec.scope_field) if spec.scope_field else None
    if raw in (None, "", "*"):
        return ["*"]
    parts = [p.strip().replace("\\", "/") for p in str(raw).split(",")]
    globs = [f"*{p}*" for p in parts if p and p != "*"]
    return globs or ["*"]


@dataclass
class MemoryRecord:
    kind: str
    id: Any
    fields: Dict[str, Any]
    text: str
    created_at: str = ""
    updated_at: str = ""
    agent: str = "unknown"
    session_id: str = ""
    source: str = "legacy"
    evidence: str = ""
    commit: str = ""
    scope: List[str] = field(default_factory=lambda: ["*"])
    priority: str = "should"
    confidence: float = 0.6
    status: str = "active"
    invalid_at: str = ""
    helpful: int = 0
    harmful: int = 0

    @property
    def ref(self) -> NodeRef:
        return (self.kind, self.id)

    @property
    def tier(self) -> str:
        return KINDS[self.kind].tier

    @classmethod
    def from_node(cls, node, warnings: Optional[List[str]] = None) -> "MemoryRecord":
        props = node.properties
        spec = KINDS[node.type]

        def warn(name: str) -> None:
            if warnings is not None:
                warnings.append(f"{node.type}:{node.id}: bad value for {name!r}, using default")

        def text_of(name: str, default: str = "") -> str:
            v = props.get(name)
            return default if v is None or v == "" else str(v)

        source = text_of("source", "legacy")

        scope = _derived_scope(spec, props)
        raw_scope = props.get("scope")
        if isinstance(raw_scope, list):
            scope = [str(x) for x in raw_scope] or scope
        elif raw_scope not in (None, ""):
            try:
                parsed = json.loads(raw_scope)
                if not isinstance(parsed, list):
                    raise ValueError
                scope = [str(x) for x in parsed] or scope
            except (ValueError, TypeError):
                warn("scope")

        confidence = SOURCE_CONFIDENCE.get(source, 0.6)
        raw_conf = props.get("confidence")
        if raw_conf not in (None, ""):
            try:
                confidence = min(1.0, max(0.0, float(raw_conf)))
            except (ValueError, TypeError):
                warn("confidence")

        def count(name: str) -> int:
            v = props.get(name)
            if v in (None, ""):
                return 0
            try:
                return max(0, int(v))
            except (ValueError, TypeError):
                warn(name)
                return 0

        priority = text_of("priority")
        if priority not in PRIORITIES:
            priority = "info" if spec.tier == BACKGROUND else "should"

        status = _STATUS_ALIASES.get(text_of("status", "active"), text_of("status", "active"))
        if status not in STATUSES:
            warn("status")
            status = "active"

        created = text_of("created_at")
        if not created:
            for name in _LEGACY_TIME_FIELDS:
                if props.get(name):
                    created = str(props[name])
                    break

        fields = {k: v for k, v in props.items() if k not in ENVELOPE_FIELDS or k == "status"}
        return cls(
            kind=node.type, id=node.id, fields=fields, text=build_text(node.type, props),
            created_at=created, updated_at=text_of("updated_at", created),
            agent=text_of("agent", "unknown"), session_id=text_of("session_id"),
            source=source, evidence=text_of("evidence"), commit=text_of("commit"),
            scope=scope, priority=priority, confidence=confidence, status=status,
            invalid_at=text_of("invalid_at"), helpful=count("helpful"), harmful=count("harmful"),
        )
