"""
Context Builder - Builds coding context from memory
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .record import MemoryRecord

if TYPE_CHECKING:
    from .memory import AgentMemory


@dataclass
class CodingContext:
    """
    Context package for a coding task.

    Contains relevant memories organized by type and priority.
    """
    task: str
    files: List[str] = field(default_factory=list)

    # High priority - must know
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    constraints: List[Dict[str, Any]] = field(default_factory=list)
    hot_paths: List[Dict[str, Any]] = field(default_factory=list)

    # Medium priority - should know
    conventions: List[Dict[str, Any]] = field(default_factory=list)
    patterns: List[Dict[str, Any]] = field(default_factory=list)
    bug_risks: List[Dict[str, Any]] = field(default_factory=list)
    dependencies: List[Dict[str, Any]] = field(default_factory=list)

    # Low priority - nice to know
    module_context: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    team_experts: List[Dict[str, Any]] = field(default_factory=list)

    # Session continuity
    session_context: Dict[str, Any] = field(default_factory=dict)

    # Metadata
    estimated_tokens: int = 0

    # Ranked items behind the lists above, when built by ContextBuilder
    items: List[Any] = field(default_factory=list)
    result: Optional[Any] = None

    def to_prompt(self) -> str:
        """Convert context to a prompt-ready string"""
        if self.result is not None:
            return self.result.to_prompt()
        return self._legacy_prompt()

    def _legacy_prompt(self) -> str:
        """Render the category lists (used for hand-built contexts)"""
        sections = []

        # High priority
        if self.warnings:
            sections.append("## WARNINGS")
            for w in self.warnings:
                sections.append(f"- {w.get('type', 'warning')}: {w.get('message', str(w))}")

        if self.constraints:
            sections.append("\n## CONSTRAINTS")
            for c in self.constraints:
                sections.append(f"- {c.get('decision', '')}: {c.get('how', '')}")

        if self.hot_paths:
            sections.append("\n## HOT PATHS (Performance Critical)")
            for hp in self.hot_paths:
                sections.append(f"- {hp.get('name', '')}: {hp.get('warning', 'untouchable')}")

        # Medium priority
        if self.conventions:
            sections.append("\n## CODE CONVENTIONS")
            for c in self.conventions:
                category = c.get('category', '')
                rule = c.get('rule', '') or c.get('preferred_form', '') or c.get('template', '')
                if rule:
                    sections.append(f"- [{category}] {rule}")

        if self.patterns:
            sections.append("\n## PROJECT PATTERNS")
            for p in self.patterns:
                sections.append(f"- {p.get('name', '')}: {p.get('template', '')}")

        if self.bug_risks:
            sections.append("\n## BUG RISKS")
            for b in self.bug_risks:
                pattern = b.get('pattern', {})
                sections.append(f"- {pattern.get('name', '')}: {pattern.get('signature', '')}")
                if b.get('fix'):
                    sections.append(f"  Fix: {b.get('fix', {}).get('fix_template', '')}")

        if self.dependencies:
            sections.append("\n## DEPENDENCY NOTES")
            for d in self.dependencies:
                quirk = d.get('quirk', {})
                sections.append(f"- {quirk.get('description', '')}")
                if d.get('workaround'):
                    sections.append(f"  Workaround: {d.get('workaround', {}).get('solution', '')}")

        # Session continuity
        if self.session_context:
            if self.session_context.get("open_questions"):
                sections.append("\n## OPEN QUESTIONS")
                for q in self.session_context["open_questions"]:
                    sections.append(f"- {q.get('question', '')}")

            if self.session_context.get("decisions"):
                sections.append("\n## SESSION DECISIONS")
                for d in self.session_context["decisions"]:
                    sections.append(f"- {d.get('decision', '')}: {d.get('rationale', '')}")

        return "\n".join(sections)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "task": self.task,
            "files": self.files,
            "warnings": self.warnings,
            "constraints": self.constraints,
            "hot_paths": self.hot_paths,
            "conventions": self.conventions,
            "patterns": self.patterns,
            "bug_risks": self.bug_risks,
            "dependencies": self.dependencies,
            "module_context": self.module_context,
            "decisions": self.decisions,
            "team_experts": self.team_experts,
            "session_context": self.session_context,
            "estimated_tokens": self.estimated_tokens,
        }


class ContextBuilder:
    """
    Builds a CodingContext from AgentMemory.recall().

    The category lists on CodingContext are a view over the ranked items,
    kept so existing callers continue to work.
    """

    # Old store name -> memory kinds it covers. Order is the legacy priority order.
    STORE_KINDS = {
        "warnings": ["alternative", "refactoring_attempt"],
        "constraints": [],            # decisions promoted by a CONSTRAINS edge
        "hot_paths": ["hot_path", "performance_constraint"],
        "conventions": ["convention", "idiom", "test_convention", "required_test_case"],
        "patterns": ["project_pattern", "template", "snippet"],
        "bug_risks": ["bug_pattern", "fix_pattern", "bug"],
        "dependencies": ["quirk", "workaround"],
        "module_context": ["module", "design_pattern", "tech_debt"],
        "decisions": ["decision"],
        "team_experts": ["expertise_area"],
    }
    STORES = [(name, 1.0) for name in STORE_KINDS]
    SESSION_KINDS = ["session_decision", "open_question", "blocker", "partial_work"]

    def __init__(self, memory: "AgentMemory"):
        self.memory = memory

    def build(
        self,
        task: str,
        files: List[str],
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        max_tokens: int = 4000,
    ) -> CodingContext:
        """Build context for a task"""
        names = include or list(self.STORE_KINDS)
        if exclude:
            names = [n for n in names if n not in exclude]
        filtered = bool(include or exclude)

        kinds = None
        if filtered:
            kinds = [k for n in names for k in self.STORE_KINDS.get(n, [])]
            if "constraints" in names and "decision" not in kinds:
                kinds.append("decision")
            kinds += self.SESSION_KINDS

        if not filtered:
            result = self.memory.recall(task=task, files=files, max_tokens=max_tokens)
        else:
            # Pinned (must/"*") and carried session items are always-on: they are kept
            # whatever `exclude` says, and dropped only when `include` is given and names
            # none of their buckets (carried items are in the "session" bucket).
            result = self.memory._retriever.recall(task=task, files=files, kinds=kinds,
                                                   max_tokens=max_tokens, always_on=True)

            def keep(item) -> bool:
                bucket = self._bucket(item)
                if item.pinned or "session" in item.channels:
                    return include is None or bucket in include
                return bucket in names + ["session"]

            result.items = [i for i in result.items if keep(i)]
            result.tokens = self.memory._retriever.counter.count(result.to_prompt())

        context = CodingContext(task=task, files=files, items=result.items, result=result,
                                estimated_tokens=result.tokens)
        context.session_context = self._session_frame()
        ranked = {item.record.ref for item in result.items}
        for item in result.items:
            self._place(context, item, ranked)
        return context

    def _session_frame(self) -> Dict[str, Any]:
        """The keys SessionStore.get_session_context returned for the latest session, so
        they are present whenever a session exists. The lists hold ranked items only."""
        graph = self.memory.graph
        latest, started = None, ""
        for node in graph.nodes("session"):
            when = str(node.properties.get("started") or "")
            if latest is None or when >= started:     # a tie goes to the later one
                latest, started = node, when
        if latest is None:
            return {}
        previous, seen, current = [], {latest.id}, ("session", latest.id)
        while len(previous) < 10:
            nxt = next((e.target for e in graph.edges("CONTINUES", source=current)
                        if e.target[1] not in seen and graph.has_node(*e.target)), None)
            if nxt is None:
                break
            seen.add(nxt[1])
            previous.append(dict(graph.get_node(*nxt).properties))
            current = nxt
        return {"session": dict(latest.properties), "previous_sessions": previous,
                "decisions": [], "partial_work": [], "open_questions": [], "blockers": []}

    def _fix_for(self, ref, ranked) -> Optional[Dict[str, Any]]:
        """The fix linked to a bug pattern by FIX_FOR: a ranked one first, else an active one."""
        graph = self.memory.graph
        fixes = []
        for edge in graph.edges("FIX_FOR", target=ref):
            if edge.source[0] == "fix_pattern" and graph.has_node(*edge.source):
                rec = MemoryRecord.from_node(graph.get_node(*edge.source))
                if rec.status == "active":
                    fixes.append(rec)
        if not fixes:
            return None
        fixes.sort(key=lambda r: (r.ref not in ranked, str(r.id)))
        return {"id": fixes[0].id, **fixes[0].fields}

    def _bucket(self, item) -> str:
        kind = item.record.kind
        if kind in self.SESSION_KINDS:
            return "session"
        if kind == "decision":
            return "constraints" if item.tier == "guardrail" else "decisions"
        for name, kinds in self.STORE_KINDS.items():
            if kind in kinds:
                return name
        return "module_context"

    def _place(self, context: CodingContext, item, ranked=frozenset()) -> None:
        rec = item.record
        props = {"id": rec.id, **rec.fields}
        bucket = self._bucket(item)
        if bucket == "session":
            key = {"open_question": "open_questions", "session_decision": "decisions",
                   "blocker": "blockers", "partial_work": "partial_work"}[rec.kind]
            context.session_context.setdefault(key, []).append(props)
        elif bucket == "warnings":
            context.warnings.append({"type": rec.kind, "message": item.text, **props})
        elif bucket == "constraints":
            context.constraints.append({"decision": rec.fields.get("title", ""),
                                        "how": item.text, **props})
        elif bucket == "hot_paths":
            context.hot_paths.append({"warning": item.text, **props})
        elif bucket == "bug_risks":
            # {"type", "pattern", "fix"} as before; a fix rides on its pattern's entry and
            # is not an entry of its own (it stays in `items` and the prompt)
            if rec.kind != "fix_pattern":
                context.bug_risks.append({"type": "bug_risk", "pattern": props,
                                          "fix": self._fix_for(rec.ref, ranked)})
        elif bucket == "dependencies":
            key = "workaround" if rec.kind == "workaround" else "quirk"
            context.dependencies.append({key: props})
        else:
            getattr(context, bucket).append({"type": rec.kind, **props})
