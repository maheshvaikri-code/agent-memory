"""
Agent Memory - Main memory manager coordinating all stores
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from .record import KINDS, MemoryRecord, NodeRef
from .storage import JournalBackend, SharedGraph, StoreInUse, atomic_write_text, transactional
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
        self._backend = None
        self._bind(ISONGraph(name=name), agent)
        self.graph.add_node("agent", "self", name="agent")

    def _bind(self, graph: ISONGraph, agent: str) -> None:
        """Attach every store, the writer, the context builder and the consolidator to `graph`."""
        self.graph = graph
        self.writer = MemoryWriter(graph, self.repo_root, agent)
        self._retriever = Retriever(graph, self.repo_root)
        self._retriever.current_session = lambda: self.writer.session_id
        for attr, cls in self._STORES:
            store = cls(graph)
            store.writer = self.writer
            setattr(self, attr, store)
        self._context_builder = ContextBuilder(self)
        self._consolidator = MemoryConsolidator(self)

    # =========================================================================
    # Session Management
    # =========================================================================

    @transactional
    def start_session(self, task: str) -> str:
        """Start a new coding session"""
        session_id = self.sessions.start_session(task)

        # Link to previous session if exists
        last = self.sessions.get_last_session()
        if last and last != session_id:
            self.sessions.continue_session(session_id, last)

        self.writer.session_id = session_id
        return session_id

    @transactional
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
        self._sync()
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

    @transactional
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
        self._sync()
        warnings = []

        # Check decisions
        decision_conflict = self.decisions.check_decision(suggestion)
        if decision_conflict:
            warnings.append({
                "type": "decision_conflict",
                **decision_conflict
            })

        # Check tech debt (failed refactoring). A list, because each store call is its own
        # transaction and may apply other processes' writes to the graph mid-loop.
        for node in list(self.graph.nodes()):
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
        self._sync()
        return self.team.find_expert(topic)

    # =========================================================================
    # Record operations
    # =========================================================================

    def set_actor(self, agent: str, session_id: Optional[str] = None) -> None:
        """Set who is stamped as the author of subsequent writes.

        `session_id` None keeps the current session; "" clears it; any other value
        stamps later writes with that session (and makes its open items the carried ones).
        """
        self.writer.agent = agent
        if session_id is not None:
            self.writer.session_id = session_id

    def recall(self, task: str = "", files: Optional[List[str]] = None,
               symbols: Optional[List[str]] = None, kinds: Optional[List[str]] = None,
               max_tokens: int = 2000, compact: bool = False) -> RecallResult:
        """Memories relevant to a task, ranked, within a token budget.

        Returns an empty result when nothing stored is relevant.
        """
        self._sync()
        return self._retriever.recall(task=task, files=files, symbols=symbols, kinds=kinds,
                                      max_tokens=max_tokens, compact=compact)

    def check(self, approach: str, files: Optional[List[str]] = None):
        """Rejected alternatives, bug patterns, failed refactors and constraints
        relevant to `approach`. Empty when nothing relevant is stored."""
        self._sync()
        return self._retriever.check(approach, files)

    def count_tokens(self, text: str) -> int:
        """Tokens in `text` by the counter recall() uses for its budget."""
        return self._retriever.counter.count(text)

    def set_token_counter(self, counter) -> None:
        """Use `counter` (a TokenCounter, e.g. TokenCounter.tiktoken()) for recall budgets."""
        self._retriever.counter = counter

    def attach_semantic(self, searcher) -> None:
        """Add an optional embedding channel. `searcher.search(query, k=...)` must return
        objects with `.node_ref` and `.score`; GraphEmbeddedMemory qualifies."""
        self._retriever.semantic = searcher

    @transactional
    def remember(self, kind: str, fields: Dict[str, Any], **kw) -> WriteResult:
        """Record a memory. See MemoryWriter.add for keyword arguments."""
        return self.writer.add(kind, fields, **kw)

    @transactional
    def supersede(self, old_id, kind: str, fields: Dict[str, Any], **kw) -> WriteResult:
        """Replace `old_id` with a new record; the old one stops being returned."""
        return self.writer.supersede(self.writer.find(old_id), kind, fields, **kw)

    @transactional
    def retract(self, memory_id, reason: str = "") -> None:
        """Mark a memory as wrong. It is kept, and no longer returned."""
        self.writer.retract(self.writer.find(memory_id), reason)

    @transactional
    def feedback(self, memory_id, helpful: bool) -> None:
        """Tell the memory whether a recalled record helped."""
        self.writer.feedback(self.writer.find(memory_id), helpful)

    @transactional
    def anchor(self, memory_id, path: str, symbol: str = "") -> None:
        """Tie a memory to a file, recording the file's current content hash."""
        anchors_mod.add_anchor(self.graph, self.writer.find(memory_id), path, symbol,
                               self.repo_root, anchors_mod.head_commit(self.repo_root))

    @transactional
    def verify(self, memory_id) -> int:
        """Confirm a memory still holds: re-stamp its anchors at the current file hashes."""
        return anchors_mod.restamp(self.graph, self.writer.find(memory_id), self.repo_root)

    # =========================================================================
    # Lessons waiting for review
    # =========================================================================

    def pending_records(self) -> List[MemoryRecord]:
        """Records a person has not accepted yet (status "pending"), oldest first. They are
        never recalled, checked or carried."""
        self._sync()
        return self.writer.pending()

    def find_duplicate(self, kind: str, fields: Dict[str, Any]) -> Optional[Tuple[NodeRef, str]]:
        """(ref, "active" | "pending") of a record with the text these fields build, or None."""
        self._sync()
        return self.writer.find_text(kind, fields)

    @transactional
    def accept(self, memory_id) -> None:
        """Promote a pending record to an active, `reviewed` note (a person's decision)."""
        self.writer.accept(self.writer.find(memory_id))

    @transactional
    def expire_pending(self, now: Optional[datetime] = None) -> int:
        """Archive pending records older than PENDING_TTL_DAYS; returns how many."""
        return self.writer.expire_pending(now)

    @transactional
    def capture(self, lessons: List[Dict[str, Any]], *, max_pending: int) -> Dict[str, Any]:
        """Store lessons as pending records, atomically across processes: sweep expired ones,
        count what is waiting, then add each lesson unless it duplicates an active or pending
        note or the queue is full. Each lesson is `{"kind", "fields", "anchors", "evidence"}`,
        already validated by the caller. Nothing here ever changes an existing record."""
        swept = self.writer.expire_pending()
        total = len(self.writer.pending())
        room = max_pending - total
        if room <= 0:
            return {"captured": [], "skipped": [], "full": True, "swept": swept, "pending": total}
        captured: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for index, item in enumerate(lessons):
            hit = self.writer.find_text(item["kind"], item["fields"])
            if hit is not None:
                skipped.append({"index": index, "code": "duplicate",
                                "of": hit[0][1] if hit[1] == "active" else None,
                                "of_status": hit[1]})
            elif room <= 0:
                skipped.append({"index": index, "code": "queue_full", "of": None,
                                "of_status": None})
            else:
                result = self.writer.add(item["kind"], item["fields"], anchors=item["anchors"],
                                         evidence=item["evidence"], source="extracted",
                                         status="pending")
                captured.append({"index": index, "id": result.id})
                room -= 1
        return {"captured": captured, "skipped": skipped, "full": False, "swept": swept,
                "pending": total + len(captured)}

    # =========================================================================
    # Persistence
    # =========================================================================

    def save(self, path: str) -> None:
        """Save memory to file (atomic). In shared mode, saving to the store's own path is a
        checkpoint; saving elsewhere writes a full snapshot copy. No instance, shared or plain,
        may overwrite another store that has a journal: open it with AgentMemory.open()."""
        target = str(path)
        real = os.path.realpath(target)         # a store's journal sits beside its real file
        if self._backend is not None:
            own_path = os.path.normcase(self._backend.path)
            if os.path.normcase(real) == own_path:
                self._backend.checkpoint()
                return
        if os.path.exists(real + ".journal"):
            raise StoreInUse(f"{target} is a shared store (it has a journal); use "
                             f"AgentMemory.open({target!r}) or save to another path")
        if self._backend is not None:
            self._sync()                  # the copy includes other processes' recent writes
        text = self.graph.to_isonl() if Path(target).suffix == ".isonl" else self.graph.to_ison()
        atomic_write_text(target, text)

    @classmethod
    def load(cls, path: str, repo_root: Optional[str] = None,
             agent: str = "unknown") -> "AgentMemory":
        """Load memory from file. A shared store is loaded as a plain snapshot with its
        journal replayed; use AgentMemory.open() to keep working on it with other processes."""
        if Path(path).suffix == ".isonl":
            graph = ISONGraph.load(path)
        elif os.path.exists(os.path.realpath(str(path)) + ".journal"):
            graph = JournalBackend.read_only(path)        # snapshot + journal, as a plain graph
        else:
            graph = SharedGraph.from_ison(Path(path).read_text(encoding="utf-8"), Path(path).stem)
        memory = cls.__new__(cls)
        memory._backend = None
        memory.name = graph.name
        memory.repo_root = str(repo_root) if repo_root else None
        memory.load_warnings = []
        memory._bind(graph, agent)
        memory.load_warnings.extend(getattr(graph, "replay_warnings", []))
        for kind in KINDS:
            for node in graph.nodes(kind):
                MemoryRecord.from_node(node, memory.load_warnings)
        return memory

    @classmethod
    def open(cls, path, repo_root: Optional[str] = None, agent: str = "unknown",
             **backend_options) -> "AgentMemory":
        """Open (or create) a store that several processes can share safely.

        Every write is durable when the call returns and visible to other processes on their
        next read. Options: durability ("flush" | "fsync"), lock_timeout, compact_min_bytes.
        """
        memory = cls.__new__(cls)
        memory._open_store(path, repo_root, agent, **backend_options)
        return memory

    def _open_store(self, path, repo_root, agent, **backend_options) -> None:
        backend = JournalBackend(path, agent=agent, **backend_options)
        graph = backend.open()
        self.name = graph.name
        self.repo_root = str(repo_root) if repo_root else None
        self.load_warnings = list(backend.warnings)
        self._backend = backend
        self._bind(graph, agent)
        with graph.transaction():
            if not graph.has_node("agent", "self"):
                graph.add_node("agent", "self", name="agent")

    @property
    def is_shared(self) -> bool:
        return self._backend is not None

    @property
    def warnings(self) -> List[str]:
        """Everything the store has to say: problems met while loading and, in shared mode, notes
        from the storage layer (a skipped compaction or close checkpoint, a journal that was
        rebuilt). A copy: appending to it changes nothing."""
        out = list(self.load_warnings)
        if self._backend is not None:
            out.extend(w for w in self._backend.warnings if w not in out)
        return out

    def carried_items(self):
        """Open questions, blockers and partial work that recall() carries: this writer's current
        session while it is open, else the most recent session that was never ended (within 14
        days). Items of a session that was ended are not carried."""
        self._sync()
        return self._retriever.carried()

    def unfolded_transactions(self) -> int:
        """Journal transactions the snapshot does not hold yet (0 for a plain memory)."""
        self._sync()
        return self._backend.unfolded() if self._backend is not None else 0

    def refresh(self) -> int:
        """Apply what other processes wrote since this one last looked."""
        return self._backend.refresh() if self._backend is not None else 0

    def checkpoint(self) -> bool:
        """Fold the journal into the snapshot. False when not shared or it was skipped."""
        return self._backend.checkpoint() if self._backend is not None else False

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()

    def __enter__(self) -> "AgentMemory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _sync(self) -> None:
        if self._backend is not None:
            self._backend.refresh_if_stale()

    def export_json(self, path: str) -> None:
        """Export memory as JSON"""
        import json
        self._sync()
        data = self.graph.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # =========================================================================
    # Statistics
    # =========================================================================

    @transactional
    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive memory statistics. In shared mode one transaction: the lock is
        taken once and every number comes from the same state."""
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
