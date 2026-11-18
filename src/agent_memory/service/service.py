"""MemoryService: the one place that talks to AgentMemory on behalf of an agent.

Transport-agnostic (the MCP server, the CLI and a later HTTP endpoint all call it). It owns
validation, size limits, path confinement, error mapping and result shaping, and returns plain
JSON-able dicts. It never imports `mcp`.
"""
from __future__ import annotations

import math
import os
import re
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..memory import AgentMemory
from ..record import ENVELOPE_FIELDS, KINDS, PRIORITIES, MemoryRecord
from ..storage.errors import StoreCorrupt, StoreInUse, StoreLockTimeout
from .config import ServiceConfig, sanitize_agent
from .errors import ServiceError
from .shaping import item_to_dict

AGENT_KINDS = ("convention", "decision", "bug_pattern", "alternative", "quirk", "workaround",
               "refactoring_attempt", "open_question", "blocker", "partial_work")
RESERVED_FIELDS = frozenset(ENVELOPE_FIELDS) | {"id"}
MAX_FIELD_CHARS = 2000
MAX_FIELDS = 12
MAX_TASK_CHARS = 2000
MAX_FILES = 50
MAX_SYMBOLS = 20
MAX_ITEMS = 50
DEFAULT_TOKENS = 1500
MIN_TOKENS, MAX_TOKENS = 200, 4000
MAX_INT_DIGITS = 18
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _required(kind: str) -> List[str]:
    seen: List[str] = []
    for name in re.findall(r"\{(\w+)\}", KINDS[kind].template):
        if name not in seen:
            seen.append(name)
    return seen


def _bad(message: str) -> ServiceError:
    return ServiceError("invalid_argument", message)


class MemoryService:
    def __init__(self, config: Optional[ServiceConfig] = None, *,
                 memory: Optional[AgentMemory] = None):
        if config is None and memory is None:
            raise ValueError("MemoryService needs a config or an existing AgentMemory")
        self._lock = threading.RLock()
        self._owns = memory is None
        self._closed = False
        if memory is not None:
            self.memory = memory
            self.config = config or ServiceConfig(
                store_path="", repo_root=str(memory.repo_root or os.getcwd()),
                agent=sanitize_agent(memory.writer.agent))
            memory.set_actor(self.config.agent)     # writes are stamped with the clean name
        else:
            self.config = config
            os.makedirs(os.path.dirname(config.store_path), exist_ok=True)
            self.memory = self._call(lambda: AgentMemory.open(
                config.store_path, repo_root=config.repo_root, agent=config.agent))
        self._root = Path(self.config.repo_root).resolve()
        self._session: Optional[str] = None

    # -- plumbing ------------------------------------------------------------

    def _call(self, fn: Callable[[], Any]) -> Any:
        """Run an engine call on an open service, turning its exceptions into service errors."""
        self._ensure_open()
        return self._translate(fn)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ServiceError("internal", "service is closed")

    @staticmethod
    def _translate(fn: Callable[[], Any]) -> Any:
        """Every exception leaves as a ServiceError. `not_found` is never produced here: only
        the service's own id lookup (_lookup) reports it, so a stray KeyError is `internal`."""
        try:
            return fn()
        except ServiceError:
            raise
        except (StoreLockTimeout, StoreInUse):
            raise ServiceError("store_busy", "another process holds the store; retry shortly") from None
        except StoreCorrupt as exc:
            raise ServiceError("store_corrupt", str(exc)) from None
        except ValueError as exc:
            raise ServiceError("invalid_argument", str(exc)) from None
        except Exception as exc:     # OSError, TypeError, KeyError, ...: no internals leak out
            raise ServiceError("internal", "the memory engine failed unexpectedly") from exc

    def _lookup(self, ident: str, *, agent_kinds_only: bool = False) -> MemoryRecord:
        """The record with this id, read after taking in other processes' writes. Raises
        not_found when there is none; with `agent_kinds_only`, invalid_argument for a record an
        agent may not change (a kind outside AGENT_KINDS, or a `must` note without allow_must)."""
        def find() -> Optional[MemoryRecord]:
            self.memory.refresh()
            graph = self.memory.graph
            for kind in KINDS:                 # the same order AgentMemory's own lookup uses
                if graph.has_node(kind, ident):
                    return MemoryRecord.from_node(graph.get_node(kind, ident))
            return None

        record = self._call(find)
        if record is None:
            raise ServiceError("not_found", f"no memory with id {ident!r}")
        if agent_kinds_only:
            if record.kind not in AGENT_KINDS:
                raise _bad(f"{ident!r} is a {record.kind} record, which agents cannot change; "
                           f"only these kinds can be: {', '.join(AGENT_KINDS)}")
            if record.priority == "must" and not self.config.allow_must:
                raise _bad(f"{ident!r} is a 'must' note; a human must change it (or start the "
                           "service with allow_must)")
        return record

    def set_client(self, name: Optional[str]) -> None:
        """Stamp later writes with the connecting client's name (an MCP client reports one)."""
        if not name:
            return
        agent = sanitize_agent(name)
        with self._lock:
            if agent != self.config.agent:
                self.config = replace(self.config, agent=agent)
                self.memory.set_actor(agent)

    # -- validation ------------------------------------------------------------

    def _text(self, name: str, value: Any, limit: int, *, allow_empty: bool = False) -> str:
        if value is None and allow_empty:
            return ""
        if not isinstance(value, str):
            raise _bad(f"{name} must be a string")
        value = value.strip()
        if not value and not allow_empty:
            raise _bad(f"{name} must not be empty")
        if len(value) > limit:
            raise ServiceError("too_large", f"{name} is longer than {limit} characters")
        return value

    def _id(self, value: Any) -> str:
        return self._text("id", value, 200)

    def _confine(self, path: Any) -> str:
        if not isinstance(path, str) or not path.strip():
            raise _bad("each file must be a non-empty path string")
        if _CONTROL.search(path):
            raise _bad(f"{path!r} contains a control character")
        candidate = Path(path)
        full = candidate if candidate.is_absolute() else self._root / candidate
        try:
            resolved = full.resolve()
            relative = resolved.relative_to(self._root)
        except ValueError:
            raise ServiceError("path_outside_repo", f"{path!r} is outside the repository") from None
        except (OSError, RuntimeError):
            raise _bad(f"{path!r} cannot be resolved") from None
        rel = relative.as_posix()
        if rel in ("", "."):
            raise _bad(f"{path!r} is the repository root; anchor a file or directory inside it")
        return rel

    def _files(self, files: Any) -> List[str]:
        if files is None:
            return []
        if not isinstance(files, (list, tuple)):
            raise _bad("files must be a list of paths")
        if len(files) > MAX_FILES:
            raise ServiceError("too_large", f"at most {MAX_FILES} files per call")
        return [self._confine(f) for f in files]

    def _fields(self, kind: Any, fields: Any) -> Dict[str, Any]:
        if kind not in AGENT_KINDS:
            raise ServiceError("unknown_kind", f"kind must be one of: {', '.join(AGENT_KINDS)}")
        if not isinstance(fields, dict) or not fields:
            raise _bad("fields must be a non-empty object")
        if len(fields) > MAX_FIELDS:
            raise ServiceError("too_large", f"at most {MAX_FIELDS} fields per record")
        clean: Dict[str, Any] = {}
        for key, value in fields.items():
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                raise _bad(f"field name {key!r} is not a plain identifier")
            if key in RESERVED_FIELDS:
                raise _bad(f"{key!r} is set by the memory itself and cannot be a field")
            if isinstance(value, bool):
                clean[key] = value
            elif isinstance(value, (int, float)):
                if isinstance(value, float) and not math.isfinite(value):
                    raise _bad(f"field {key!r} must be a finite number")
                if isinstance(value, int) and abs(value) >= 10 ** MAX_INT_DIGITS:
                    # compared numerically: str() of a 5000-digit int raises on Python 3.11+
                    raise ServiceError("too_large",
                                       f"field {key!r} has more than {MAX_INT_DIGITS} digits")
                clean[key] = value
            elif isinstance(value, str):
                if len(value) > MAX_FIELD_CHARS:
                    raise ServiceError("too_large",
                                       f"field {key!r} is longer than {MAX_FIELD_CHARS} characters")
                clean[key] = value.strip()
            else:
                raise _bad(f"field {key!r} must be a string or a number")
        missing = [name for name in _required(kind) if not str(clean.get(name, "")).strip()]
        if missing:
            raise _bad(f"a {kind} needs: {', '.join(missing)}")
        return clean

    def _budget(self, max_tokens: Any) -> int:
        if max_tokens is None:
            return DEFAULT_TOKENS
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise _bad("max_tokens must be an integer")
        return max(MIN_TOKENS, min(MAX_TOKENS, max_tokens))

    # -- writes ----------------------------------------------------------------

    def remember(self, kind: str, fields: Dict[str, Any], files: Optional[List[str]] = None,
                 evidence: str = "", confidence: Optional[float] = None,
                 priority: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            clean = self._fields(kind, fields)
            anchors = self._files(files)
            evidence = self._text("evidence", evidence, MAX_FIELD_CHARS, allow_empty=True)
            if confidence is not None:
                if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                        or not 0.0 <= float(confidence) <= 1.0):
                    raise _bad("confidence must be a number between 0 and 1")
            if priority is not None:
                if priority not in PRIORITIES:
                    raise _bad(f"priority must be one of {', '.join(PRIORITIES)}")
                if priority == "must" and not self.config.allow_must:
                    raise _bad("priority 'must' pins a note into every recall and needs the "
                               "service to be started with allow_must (a human decision)")
            result = self._call(lambda: self.memory.remember(
                kind, clean, anchors=anchors, evidence=evidence, confidence=confidence,
                priority=priority, source="explicit"))
            return {"id": str(result.id), "kind": kind, "created": bool(result.created),
                    "merged": bool(result.merged), "similar": [str(ref[1]) for ref in result.similar]}

    def feedback(self, id: str, helpful: bool) -> Dict[str, Any]:
        with self._lock:
            ident = self._id(id)
            if not isinstance(helpful, bool):
                raise _bad("helpful must be true or false")
            self._lookup(ident)
            self._call(lambda: self.memory.feedback(ident, helpful))
            return {"ok": True, "id": ident}

    def supersede(self, old_id: str, kind: str, fields: Dict[str, Any],
                  files: Optional[List[str]] = None, evidence: str = "") -> Dict[str, Any]:
        with self._lock:
            old = self._id(old_id)
            clean = self._fields(kind, fields)
            anchors = self._files(files)
            evidence = self._text("evidence", evidence, MAX_FIELD_CHARS, allow_empty=True)
            record = self._lookup(old, agent_kinds_only=True)
            result = self._call(lambda: self.memory.supersede(
                old, kind, clean, anchors=anchors, evidence=evidence, source="explicit"))
            # the same text again resolves to the old record itself: nothing was superseded
            replaced = None if tuple(result.ref) == tuple(record.ref) else old
            return {"id": str(result.id), "kind": kind, "superseded": replaced,
                    "created": bool(result.created)}

    def retract(self, id: str, reason: str = "") -> Dict[str, Any]:
        with self._lock:
            ident = self._id(id)
            reason = self._text("reason", reason, MAX_FIELD_CHARS, allow_empty=True)
            self._lookup(ident, agent_kinds_only=True)
            self._call(lambda: self.memory.retract(ident, reason))
            return {"ok": True, "id": ident}

    def verify(self, id: str) -> Dict[str, Any]:
        with self._lock:
            ident = self._id(id)
            self._lookup(ident)
            count = self._call(lambda: self.memory.verify(ident))
            return {"id": ident, "restamped": int(count)}

    # -- sessions --------------------------------------------------------------

    def session_start(self, task: str) -> Dict[str, Any]:
        with self._lock:
            self._ensure_open()
            task = self._text("task", task, MAX_TASK_CHARS)
            if self._session is not None:
                return {"session_id": self._session, "already_active": True, "carried": []}
            carried = [item_to_dict(i) for i in self._call(self.memory.carried_items)]
            session_id = self._call(lambda: self.memory.start_session(task))
            self._session = session_id
            return {"session_id": session_id, "already_active": False, "carried": carried}

    def use_session(self, session_id: str) -> Dict[str, Any]:
        """Continue a session started earlier (the CLI does this across invocations)."""
        with self._lock:
            ident = self._id(session_id)

            def is_open() -> bool:
                self.memory.refresh()          # it may have been started by another process
                graph = self.memory.graph
                return graph.has_node("session", ident) and not graph.get_node(
                    "session", ident).properties.get("ended")

            if not self._call(is_open):
                raise ServiceError("not_found", f"no open session {ident!r}")
            self.memory.set_actor(self.config.agent, ident)
            self._session = ident
            return {"session_id": ident}

    def session_end(self, summary: str = "", completion: float = 1.0) -> Dict[str, Any]:
        with self._lock:
            self._ensure_open()
            if self._session is None:
                raise ServiceError("no_active_session", "no session is active; start one first")
            if (isinstance(completion, bool) or not isinstance(completion, (int, float))
                    or not 0.0 <= float(completion) <= 1.0):
                raise _bad("completion must be a number between 0 and 1")
            summary = self._text("summary", summary, MAX_FIELD_CHARS, allow_empty=True)
            unresolved = [item_to_dict(i) for i in self._call(self.memory.carried_items)]
            session_id = self._session
            if summary:
                self._call(lambda: self.memory.remember(
                    "session_decision", {"decision": summary, "rationale": "end-of-session summary"},
                    source="explicit"))
            self._call(lambda: self.memory.end_session(session_id, float(completion)))
            self._session = None
            self.memory.set_actor(self.config.agent, "")
            return {"ok": True, "session_id": session_id, "unresolved": unresolved}

    # -- status and lifecycle --------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return self._call(self._status)

    def _status(self) -> Dict[str, Any]:
        self.memory.refresh()
        counts: Dict[str, int] = {}
        last = ""
        for kind in KINDS:
            for node in self.memory.graph.nodes(kind):
                record = MemoryRecord.from_node(node)
                if record.status == "active":
                    counts[kind] = counts.get(kind, 0) + 1
                    last = max(last, record.updated_at or record.created_at or "")
        return {"store": self.config.store_path, "repo_root": self.config.repo_root,
                "agent": self.config.agent, "shared": bool(self.memory.is_shared),
                "session": self._session, "counts": counts,
                "total_active": sum(counts.values()), "last_write": last,
                "unfolded_transactions": self.memory.unfolded_transactions(),
                "warnings": self.memory.warnings, "schema": 1}

    def close(self) -> None:
        """Leave a complete snapshot behind and release the store (only if this service opened
        it). Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True                 # later calls fail with "service is closed"
            if not self._owns:
                return

            def shut() -> None:
                try:
                    if self.memory.unfolded_transactions():
                        self.memory.checkpoint()
                finally:
                    self.memory.close()

            self._translate(shut)
