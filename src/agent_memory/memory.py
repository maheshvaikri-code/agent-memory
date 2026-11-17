"""
Agent Memory - Main memory manager coordinating all stores
"""

from typing import Any, Dict, List, Optional
from pathlib import Path as FilePath

from ison_graph import ISONGraph

from .stores import (
    CodebaseStore,
    DecisionStore,
    ConventionStore,
    BugPatternStore,
    DependencyStore,
    TestPatternStore,
    PerformanceStore,
    TechDebtStore,
    TeamStore,
    SessionStore,
    TemplateStore,
    SkillStore,
    TodoStore,
    GoalStore,
    IssueStore,
)
from .context import ContextBuilder, CodingContext
from .consolidation import MemoryConsolidator
from . import anchors as anchors_mod
from .record import KINDS, MemoryRecord
from .writer import MemoryWriter, WriteResult
from .retrieval import RecallResult, Retriever


class AgentMemory:
    """
    Main entry point for the Agent Memory System.

    Provides unified access to all memory stores and context building.

    Usage:
        memory = AgentMemory("my-project")

        # Access stores directly
        memory.conventions.add("naming", "snake_case", "ruff")
        memory.bugs.add_pattern("race_condition", "shared_state_no_lock")

        # Get coding context
        context = memory.get_context(
            task="modify the cache module",
            files=["src/cache.py"]
        )

        # Save/load memory
        memory.save("project.memory")
        memory = AgentMemory.load("project.memory")
    """

    _STORES = (
        ("codebase", CodebaseStore), ("decisions", DecisionStore),
        ("conventions", ConventionStore), ("bugs", BugPatternStore),
        ("dependencies", DependencyStore), ("tests", TestPatternStore),
        ("performance", PerformanceStore), ("techdebt", TechDebtStore),
        ("team", TeamStore), ("sessions", SessionStore),
        ("templates", TemplateStore), ("skills", SkillStore),
        ("todos", TodoStore), ("goals", GoalStore), ("issues", IssueStore),
    )

    def __init__(self, name: str = "project", repo_root: Optional[str] = None,
                 agent: str = "unknown"):
        self.name = name
        self.repo_root = str(repo_root) if repo_root else None
        self.load_warnings: List[str] = []
        self._bind(ISONGraph(name=name), agent)
        self.graph.add_node("agent", "self", name="agent")

    def _bind(self, graph: ISONGraph, agent: str) -> None:
        """Attach every store, the writer, the context builder and the consolidator to `graph`."""
        self.graph = graph
        self.writer = MemoryWriter(graph, self.repo_root, agent)
        self._retriever = Retriever(graph, self.repo_root)
        for attr, cls in self._STORES:
            store = cls(graph)
            store.writer = self.writer
            setattr(self, attr, store)
        self._context_builder = ContextBuilder(self)
        self._consolidator = MemoryConsolidator(self)

    # =========================================================================
    # Session Management
    # =========================================================================

    def start_session(self, task: str) -> str:
        """Start a new coding session"""
        session_id = self.sessions.start_session(task)

        # Link to previous session if exists
        last = self.sessions.get_last_session()
        if last and last != session_id:
            self.sessions.continue_session(session_id, last)

        self.writer.session_id = session_id
        return session_id

    def end_session(self, session_id: str, completion: float = 1.0) -> None:
        """End the current session"""
        self.sessions.end_session(session_id, completion)

        # Consolidate memory after session
        self._consolidator.consolidate()

    # =========================================================================
    # Context Building
    # =========================================================================

    def get_context(
        self,
        task: str,
        files: Optional[List[str]] = None,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        max_tokens: int = 4000,
    ) -> CodingContext:
        """
        Get relevant context for a coding task.

        Args:
            task: Description of the task
            files: Files being modified
            include: Memory types to include (default: all)
            exclude: Memory types to exclude
            max_tokens: Maximum estimated tokens for context

        Returns:
            CodingContext with relevant memories
        """
        return self._context_builder.build(
            task=task,
            files=files or [],
            include=include,
            exclude=exclude,
            max_tokens=max_tokens,
        )

    def get_quick_context(self, task: str, files: Optional[List[str]] = None) -> str:
        """Get a quick summary context string"""
        context = self.get_context(task, files, max_tokens=1000)
        return context.to_prompt()

    # =========================================================================
    # Memory Recording (convenience methods)
    # =========================================================================

    def record(self, event_type: str, **kwargs) -> None:
        """
        Generic event recording.

        Automatically routes to appropriate store based on event type.
        """
        if event_type == "bug_prevented":
            self.bugs.record_prevention(
                kwargs.get("pattern", ""),
                kwargs.get("location", "")
            )
        elif event_type == "bug_missed":
            self.bugs.record_miss(
                kwargs.get("pattern", ""),
                kwargs.get("consequence", ""),
                kwargs.get("post_mortem", "")
            )
        elif event_type == "decision_violated":
            self.decisions.record_violation(
                kwargs.get("decision", ""),
                kwargs.get("suggestion", ""),
                kwargs.get("correction", "")
            )
        elif event_type == "convention_violated":
            self.conventions.record_violation(
                kwargs.get("convention_id", ""),
                kwargs.get("pattern", "")
            )
        elif event_type == "skill_applied":
            self.skills.record_skill_application(
                kwargs.get("skill", ""),
                kwargs.get("session_id", ""),
                kwargs.get("success", True),
                kwargs.get("notes", "")
            )
        elif event_type == "learning":
            self.skills.record_learning_event(
                kwargs.get("skill", ""),
                kwargs.get("type", "success"),
                kwargs.get("context", ""),
                kwargs.get("lesson", "")
            )

    # =========================================================================
    # Queries
    # =========================================================================

    def check_suggestion(self, suggestion: str) -> List[Dict[str, Any]]:
        """
        Check a suggestion against memory for potential issues.

        Returns warnings if suggestion conflicts with decisions,
        matches failed refactoring attempts, etc.
        """
        warnings = []

        # Check decisions
        decision_conflict = self.decisions.check_decision(suggestion)
        if decision_conflict:
            warnings.append({
                "type": "decision_conflict",
                **decision_conflict
            })

        # Check tech debt (failed refactoring)
        for node in self.graph.nodes():
            if node.type == "tech_debt":
                check = self.techdebt.check_suggestion(
                    node.properties.get("location", ""),
                    suggestion
                )
                if check:
                    warnings.append(check)

        # Ranked guardrails, in addition to the exact matches above.
        for item in self.check(suggestion):
            warnings.append({
                "type": "guardrail",
                "id": item.record.id,
                "kind": item.record.kind,
                "message": item.text,
                "score": item.score,
            })

        return warnings

    def who_knows(self, topic: str) -> Optional[Dict[str, Any]]:
        """Find who has expertise on a topic"""
        return self.team.find_expert(topic)

    # =========================================================================
    # Record operations
    # =========================================================================

    def set_actor(self, agent: str, session_id: str = "") -> None:
        """Set who is stamped as the author of subsequent writes."""
        self.writer.agent = agent
        self.writer.session_id = session_id

    def recall(self, task: str = "", files: Optional[List[str]] = None,
               symbols: Optional[List[str]] = None, kinds: Optional[List[str]] = None,
               max_tokens: int = 2000, compact: bool = False) -> RecallResult:
        """Memories relevant to a task, ranked, within a token budget.

        Returns an empty result when nothing stored is relevant.
        """
        return self._retriever.recall(task=task, files=files, symbols=symbols, kinds=kinds,
                                      max_tokens=max_tokens, compact=compact)

    def check(self, approach: str, files: Optional[List[str]] = None):
        """Rejected alternatives, bug patterns, failed refactors and constraints
        relevant to `approach`. Empty when nothing relevant is stored."""
        return self._retriever.check(approach, files)

    def attach_semantic(self, searcher) -> None:
        """Add an optional embedding channel. `searcher.search(query, k=...)` must return
        objects with `.node_ref` and `.score`; GraphEmbeddedMemory qualifies."""
        self._retriever.semantic = searcher

    def remember(self, kind: str, fields: Dict[str, Any], **kw) -> WriteResult:
        """Record a memory. See MemoryWriter.add for keyword arguments."""
        return self.writer.add(kind, fields, **kw)

    def supersede(self, old_id, kind: str, fields: Dict[str, Any], **kw) -> WriteResult:
        """Replace `old_id` with a new record; the old one stops being returned."""
        return self.writer.supersede(self.writer.find(old_id), kind, fields, **kw)

    def retract(self, memory_id, reason: str = "") -> None:
        """Mark a memory as wrong. It is kept, and no longer returned."""
        self.writer.retract(self.writer.find(memory_id), reason)

    def feedback(self, memory_id, helpful: bool) -> None:
        """Tell the memory whether a recalled record helped."""
        self.writer.feedback(self.writer.find(memory_id), helpful)

    def anchor(self, memory_id, path: str, symbol: str = "") -> None:
        """Tie a memory to a file, recording the file's current content hash."""
        anchors_mod.add_anchor(self.graph, self.writer.find(memory_id), path, symbol,
                               self.repo_root, anchors_mod.head_commit(self.repo_root))

    def verify(self, memory_id) -> int:
        """Confirm a memory still holds: re-stamp its anchors at the current file hashes."""
        return anchors_mod.restamp(self.graph, self.writer.find(memory_id), self.repo_root)

    # =========================================================================
    # Persistence
    # =========================================================================

    def save(self, path: str) -> None:
        """Save memory to file"""
        self.graph.save(path)

    @classmethod
    def load(cls, path: str, repo_root: Optional[str] = None,
             agent: str = "unknown") -> "AgentMemory":
        """Load memory from file"""
        graph = ISONGraph.load(path)
        memory = cls.__new__(cls)
        memory.name = graph.name
        memory.repo_root = str(repo_root) if repo_root else None
        memory.load_warnings = []
        memory._bind(graph, agent)
        for kind in KINDS:
            for node in graph.nodes(kind):
                MemoryRecord.from_node(node, memory.load_warnings)
        return memory

    def export_json(self, path: str) -> None:
        """Export memory as JSON"""
        import json
        data = self.graph.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive memory statistics"""
        return {
            "name": self.name,
            "total_nodes": self.graph.node_count(),
            "total_edges": self.graph.edge_count(),
            "stores": {
                "codebase": self.codebase.get_stats(),
                "decisions": self.decisions.get_stats(),
                "conventions": self.conventions.get_stats(),
                "bugs": self.bugs.get_stats(),
                "dependencies": self.dependencies.get_stats(),
                "tests": self.tests.get_stats(),
                "performance": self.performance.get_stats(),
                "techdebt": self.techdebt.get_stats(),
                "team": self.team.get_stats(),
                "sessions": self.sessions.get_stats(),
                "templates": self.templates.get_stats(),
                "skills": self.skills.get_stats(),
                "todos": self.todos.get_stats(),
                "goals": self.goals.get_stats(),
                "issues": self.issues.get_stats(),
            }
        }

    def __repr__(self) -> str:
        return f"AgentMemory(name={self.name}, nodes={self.graph.node_count()}, edges={self.graph.edge_count()})"
