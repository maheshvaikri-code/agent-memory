"""MemoryService: the one place that talks to AgentMemory on behalf of an agent.

Transport-agnostic (the MCP server, the CLI and a later HTTP endpoint all call it). It owns
validation, size limits, path confinement, error mapping and result shaping, and returns plain
JSON-able dicts. It never imports `mcp`.
"""
from __future__ import annotations

import logging
import math
import os
import re
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..memory import AgentMemory
from ..record import ENVELOPE_FIELDS, KINDS, PRIORITIES, MemoryRecord, pending_expired
from ..retrieval.render import render
from ..storage import JournalBackend, journal
from ..storage.errors import StoreCorrupt, StoreInUse, StoreLockTimeout
from .config import ServiceConfig, sanitize_agent
from .errors import ServiceError
from .shaping import (UNREVIEWED_NOTE, escape_id, item_to_dict, neutralize, unescape_id, wrap,
                      wrap_items)

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
MAX_CONTEXT_CHARS = 2000
WRAPPER_TOKENS = 110      # the labelled untrusted block's own cost
MIN_INNER_TOKENS = 90
UNBOUNDED_TOKENS = 10 ** 6   # only to learn whether an empty recall dropped notes for budget
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_LESSONS_PER_CALL = 10
MAX_PENDING = 100
_LESSON_KEYS = frozenset(("kind", "fields", "evidence", "files", "symbols"))
_log = logging.getLogger(__name__)
_CONTEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # \t \n \r pass
MAX_CONTEXT_TERMS = 40
_WORDISH = re.compile(r"[A-Za-z0-9_]+")
# fooBar, StripeError, HTTPError. Used with .match(): anchored, no nested repeats, so it is
# linear (the unanchored `[a-z].*[A-Z]` was quadratic on a long all-lower-case word).
_MIXED_CASE = re.compile(r"[^a-z]*[a-z][^A-Z]*[A-Z]|.*?[A-Z][A-Z][a-z]")
_NOISE_WORDS = frozenset("""
err error errors warn warning warnings info debug trace fatal critical fail failed failure
failures pass passed ok skip skipped xfail xpass note todo fixme exit status code test tests
""".split())       # log levels, test-runner words and test_ name prefixes: in every trace


def _context_terms(context: str) -> List[str]:
    """The distinctive terms of free-text context: identifier-like words only.

    Prose and log words ("error", "failed", "request", "check") would match some record in
    almost any store and end recall's abstention, so they are dropped. Kept: mixed-case
    identifiers (folded whole, lower-cased: "StripeSignatureError" -> "stripesignatureerror",
    which meets the record's own compound token but not every note that says "error"),
    the 4+ letter parts of snake_case names, words mixing letters and digits ("512MB",
    "E11000") and ALL-CAPS codes ("ECONNRESET", "JWT"). Chunks containing a path separator
    are skipped: paths go in `files`, and their segments (home, src, lib) are noise."""
    terms: List[str] = []
    for chunk in context.split():
        if "/" in chunk or "\\" in chunk:
            continue
        for word in _WORDISH.findall(chunk):
            core = word.strip("_")
            if not core or core.isdigit():
                continue
            if "_" in core:
                found = [p.lower() for p in core.split("_") if len(p) >= 4 and not p.isdigit()]
            elif _MIXED_CASE.match(core) or (any(c.isdigit() for c in core)
                                              and any(c.isalpha() for c in core)):
                found = [core.lower()]
            elif core.isupper() and len(core) >= 3:
                found = [core.lower()]
            else:
                found = []
            terms.extend(t for t in found if t not in _NOISE_WORDS)
    return list(dict.fromkeys(terms))[:MAX_CONTEXT_TERMS]


def _required(kind: str) -> List[str]:
    seen: List[str] = []
    for name in re.findall(r"\{(\w+)\}", KINDS[kind].template):
        if name not in seen:
            seen.append(name)
    return seen


def _bad(message: str) -> ServiceError:
    return ServiceError("invalid_argument", message)


class MemoryService:
    """With `config.read_only` the service never writes the store: see _load_read_only, the
    write methods (each refuses first) and close()."""

    def __init__(self, config: Optional[ServiceConfig] = None, *,
                 memory: Optional[AgentMemory] = None):
        if config is None and memory is None:
            raise ValueError("MemoryService needs a config or an existing AgentMemory")
        self._lock = threading.RLock()
        self._owns = memory is None
        self._closed = False
        self._store_exists = True
        if memory is not None:
            self.memory = memory
            self.config = config or ServiceConfig(
                store_path="", repo_root=str(memory.repo_root or os.getcwd()),
                agent=sanitize_agent(memory.writer.agent))
            memory.set_actor(self.config.agent)     # writes are stamped with the clean name
        elif config.read_only:
            self.config = config
            self.memory = self._call(self._load_read_only)
        else:
            self.config = config
            os.makedirs(os.path.dirname(config.store_path), exist_ok=True)
            self.memory = self._call(lambda: AgentMemory.open(
                config.store_path, repo_root=config.repo_root, agent=config.agent))
        self._root = Path(self.config.repo_root).resolve()
        self._session: Optional[str] = None

    def _load_read_only(self) -> AgentMemory:
        """The store as it is on disk, taking no write path: the snapshot with the journal
        replayed (AgentMemory.load, which holds the store's lock only while it reads), never
        opened, so nothing is created, converted or checkpointed. A missing store is an empty
        memory that exists only here. Each call reads afresh (one per CLI command)."""
        path, config = self.config.store_path, self.config
        JournalBackend(path)              # the writer's own path checks (.isonl); touches no file
        real = os.path.realpath(path)
        if os.path.exists(real):
            try:
                return AgentMemory.load(path, repo_root=config.repo_root, agent=config.agent)
            except (StoreCorrupt, StoreLockTimeout, StoreInUse, OSError):
                raise
            except Exception as exc:          # what open() reports for an unreadable snapshot
                raise StoreCorrupt(f"snapshot {real} cannot be read: "
                                   f"{type(exc).__name__}") from None
        tail = journal.read_tail(real + ".journal", 0)
        if tail.error is not None or tail.txs or tail.corrupt_at is not None:
            # as open() refuses it: acknowledged writes without their snapshot
            raise StoreCorrupt(f"journal {real}.journal exists without its snapshot {real}: "
                               "restore the snapshot from a backup; nothing was changed")
        self._store_exists = False
        return AgentMemory(Path(path).stem or "project", repo_root=config.repo_root,
                           agent=config.agent)

    def _refuse_if_read_only(self, action: str) -> None:
        """Defence in depth: a read-only service cannot write through any method, whatever an
        adapter asks of it."""
        if self.config.read_only:
            raise _bad(f"this service is read-only (opened for status, recall and guard): it "
                       f"cannot {action}; use a command that writes")

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
        # items[].id arrives with '<' escaped (shaping.escape_id): undo exactly that
        return unescape_id(self._text("id", value, 200))

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
            self._refuse_if_read_only("remember a note")
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
            self._refuse_if_read_only("record feedback")
            ident = self._id(id)
            if not isinstance(helpful, bool):
                raise _bad("helpful must be true or false")
            self._lookup(ident)
            self._call(lambda: self.memory.feedback(ident, helpful))
            return {"ok": True, "id": ident}

    def supersede(self, old_id: str, kind: str, fields: Dict[str, Any],
                  files: Optional[List[str]] = None, evidence: str = "") -> Dict[str, Any]:
        with self._lock:
            self._refuse_if_read_only("supersede a note")
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
            self._refuse_if_read_only("retract a note")
            ident = self._id(id)
            reason = self._text("reason", reason, MAX_FIELD_CHARS, allow_empty=True)
            self._lookup(ident, agent_kinds_only=True)
            self._call(lambda: self.memory.retract(ident, reason))
            return {"ok": True, "id": ident}

    def verify(self, id: str) -> Dict[str, Any]:
        with self._lock:
            self._refuse_if_read_only("verify a note")
            ident = self._id(id)
            self._lookup(ident)
            count = self._call(lambda: self.memory.verify(ident))
            return {"id": ident, "restamped": int(count)}

    # -- lessons (pending until a person accepts) --------------------------------

    def _lesson(self, lesson: Any) -> Dict[str, Any]:
        """One lesson, validated by the same rules as `remember`, plus: evidence is required
        and at least one file or symbol. Raises ServiceError; the caller skips the lesson."""
        if not isinstance(lesson, dict):
            raise _bad("each lesson must be an object")
        extra = sorted(str(key) for key in lesson if key not in _LESSON_KEYS)
        if extra:
            raise _bad(f"unknown lesson key(s): {', '.join(extra)}; "
                       "allowed: kind, fields, evidence, files, symbols")
        clean = self._fields(lesson.get("kind"), lesson.get("fields"))
        evidence = self._text("evidence", lesson.get("evidence"), MAX_FIELD_CHARS)
        anchors = self._files(lesson.get("files"))
        symbols = self._symbols(lesson.get("symbols"))
        if not anchors and not symbols:
            raise _bad("a lesson needs at least one file or symbol it is about")
        if symbols:
            evidence = f"{evidence}; symbols: {', '.join(symbols)}"
        return {"kind": lesson["kind"], "fields": clean, "anchors": anchors, "evidence": evidence}

    @staticmethod
    def _skip_reason(item: Dict[str, Any]) -> str:
        if item["code"] == "queue_full":
            return "queue_full: the review queue filled up; a person must review it first"
        if item["of_status"] == "active":
            return (f"duplicate: an active note with this text already exists "
                    f"({escape_id(str(item['of']))})")
        return "duplicate: this lesson is already waiting for review"

    def capture(self, lessons: Any) -> Dict[str, Any]:
        """Leave lessons for a person to review. They are stored pending: never recalled,
        checked or carried until someone accepts them with `amem review accept`. Each lesson
        that fails the gate is skipped with a reason; the others are stored."""
        with self._lock:
            self._refuse_if_read_only("capture lessons")
            self._ensure_open()
            if not isinstance(lessons, (list, tuple)) or not lessons:
                raise _bad("lessons must be a non-empty list")
            if len(lessons) > MAX_LESSONS_PER_CALL:
                raise ServiceError("too_large",
                                   f"at most {MAX_LESSONS_PER_CALL} lessons per call")
            prepared: List[Any] = []
            skipped: List[Dict[str, Any]] = []
            for index, lesson in enumerate(lessons):
                try:
                    prepared.append((index, self._lesson(lesson)))
                except ServiceError as exc:
                    skipped.append({"index": index,
                                    "reason": neutralize(f"{exc.code}: {exc.message}")})
            captured: List[str] = []
            pending = None
            if prepared:
                outcome = self._call(lambda: self.memory.capture(
                    [item for _, item in prepared], max_pending=MAX_PENDING))
                if outcome["full"]:
                    raise ServiceError(
                        "queue_full", f"{MAX_PENDING} lessons are already waiting for review; "
                        "a person must accept or reject some (amem review list) before more "
                        "can be captured")
                captured = [escape_id(str(c["id"])) for c in outcome["captured"]]
                for item in outcome["skipped"]:
                    skipped.append({"index": prepared[item["index"]][0],
                                    "reason": neutralize(self._skip_reason(item))})
                pending = outcome["pending"]
            if pending is None:
                pending = self._call(self._pending_counts)[0]
            return {"captured": captured, "skipped": sorted(skipped, key=lambda s: s["index"]),
                    "pending": pending}

    def _pending_counts(self) -> Tuple[int, int]:
        """(waiting, expired but not yet swept)."""
        live = expired = 0
        for record in self.memory.pending_records():
            if pending_expired(record.created_at):
                expired += 1
            else:
                live += 1
        return live, expired

    # -- sessions --------------------------------------------------------------

    def session_start(self, task: str) -> Dict[str, Any]:
        with self._lock:
            self._refuse_if_read_only("start a session")
            self._ensure_open()
            task = self._text("task", task, MAX_TASK_CHARS)
            pending = self._call(lambda: self._pending_counts()[0])
            if self._session is not None:
                return {"session_id": self._session, "already_active": True, "carried": [],
                        "pending_review": pending}
            carried = [item_to_dict(i) for i in self._call(self.memory.carried_items)]
            session_id = self._call(lambda: self.memory.start_session(task))
            self._session = session_id
            return {"session_id": session_id, "already_active": False, "carried": carried,
                    "pending_review": pending}

    def use_session(self, session_id: str) -> Dict[str, Any]:
        """Continue a session started earlier (the CLI does this across invocations). Allowed on
        a read-only service: it writes nothing, it only selects the session whose open items a
        recall carries (`amem recall --session`)."""
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

    def session_end(self, summary: str = "", completion: float = 1.0,
                    lessons: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        with self._lock:
            self._refuse_if_read_only("end a session")
            self._ensure_open()
            if self._session is None:
                raise ServiceError("no_active_session", "no session is active; start one first")
            if (isinstance(completion, bool) or not isinstance(completion, (int, float))
                    or not 0.0 <= float(completion) <= 1.0):
                raise _bad("completion must be a number between 0 and 1")
            summary = self._text("summary", summary, MAX_FIELD_CHARS, allow_empty=True)
            unresolved = [item_to_dict(i) for i in self._call(self.memory.carried_items)]
            session_id = self._session
            capture = None
            if lessons is not None and not (isinstance(lessons, (list, tuple)) and not lessons):
                # stamped with this session, and never able to stop the session from ending
                try:
                    capture = self.capture(lessons)
                except ServiceError as exc:
                    if exc.code == "internal":
                        _log.error("internal failure capturing lessons: %s", exc.message,
                                   exc_info=exc.__cause__ or exc)
                    capture = {"error": exc.to_dict()}
            if summary:
                self._call(lambda: self.memory.remember(
                    "session_decision", {"decision": summary, "rationale": "end-of-session summary"},
                    source="explicit"))
            self._call(lambda: self.memory.end_session(session_id, float(completion)))
            self._session = None
            self.memory.set_actor(self.config.agent, "")
            result = {"ok": True, "session_id": session_id, "unresolved": unresolved}
            if capture is not None:
                result["capture"] = capture
            return result

    # -- reads -----------------------------------------------------------------

    def _symbols(self, symbols: Any) -> List[str]:
        if symbols is None:
            return []
        if not isinstance(symbols, (list, tuple)):
            raise _bad("symbols must be a list of strings")
        if len(symbols) > MAX_SYMBOLS:
            raise ServiceError("too_large", f"at most {MAX_SYMBOLS} symbols per call")
        return [self._text("symbol", s, 100) for s in symbols]

    def _context(self, context: Any) -> str:
        """Free text the agent already has at hand (error output, a diff hunk, its goal)."""
        context = self._text("context", context, MAX_CONTEXT_CHARS, allow_empty=True)
        if _CONTEXT_CONTROL.search(context):
            raise _bad("context contains a control character "
                       "(only line breaks and tabs are allowed)")
        return context

    def recall(self, task: str, files: Optional[List[str]] = None,
               symbols: Optional[List[str]] = None,
               max_tokens: Optional[int] = None,
               context: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            task = self._text("task", task, MAX_TASK_CHARS, allow_empty=True)
            paths = self._files(files)
            names = self._symbols(symbols)
            budget = self._budget(max_tokens)
            terms = _context_terms(self._context(context))
            if terms:
                # one extra "symbol": the engine joins task and symbols into a single query
                names = names + [" ".join(terms)]
            def ask(limit: int):
                return self._call(lambda: self.memory.recall(
                    task=task, files=paths, symbols=names, max_tokens=limit))

            result = ask(max(MIN_INNER_TOKENS, budget - WRAPPER_TOKENS))
            # Neutralizing can lengthen a note ('<' -> '&lt;', NFKC expansions), so pack the
            # engine's items again against the wrapped block: in rank order, skipping an item
            # that does not fit and going on (as the engine's own packer does), so one long
            # note cannot starve the others. Items the engine itself skipped for its budget
            # are invisible here and not counted in the "did not fit" note (by design).
            # (each item is rendered and neutralized once: `pieces`; the trial is assembled)
            kept: List[Any] = []
            prompt = ""
            pieces: Dict[int, str] = {}
            for item in result.items[:MAX_ITEMS]:
                trial = wrap_items(kept + [item], result.compact, pieces)
                if self.memory.count_tokens(trial) <= budget:
                    kept.append(item)
                    prompt = trial
            while kept and self.memory.count_tokens(prompt) > budget:      # backstop
                kept.pop()
                prompt = wrap(render(kept, result.compact)) if kept else ""
            dropped = len(result.items[:MAX_ITEMS]) - len(kept)
            if not result.items:
                # empty: nothing relevant, or relevant notes the engine could not fit at all.
                # This second ranking fires the retriever's `sink` a second time on an empty
                # result: latent until a usage sink is wired; fix then with a probe/suppress
                # flag on the engine call.
                dropped = len(ask(UNBOUNDED_TOKENS).items[:MAX_ITEMS])
            items = [item_to_dict(i) for i in kept]
            if not dropped:
                note = "" if items else "nothing relevant is recorded for this task"
            elif items:
                note = (f"{dropped} more relevant note(s) did not fit in max_tokens={budget}; "
                        "ask again with a larger max_tokens to see them")
            else:
                note = (f"relevant notes exist but did not fit in max_tokens={budget}; "
                        "ask again with a larger max_tokens")
            return {"items": items, "prompt": prompt,
                    "tokens": self.memory.count_tokens(prompt) if prompt else 0,
                    "note": note}

    def check(self, approach: str, files: Optional[List[str]] = None) -> Dict[str, Any]:
        with self._lock:
            approach = self._text("approach", approach, MAX_TASK_CHARS)
            paths = self._files(files)
            found = self._call(lambda: self.memory.check(approach, files=paths))[:MAX_ITEMS]
            items = [item_to_dict(i) for i in found]
            if any(i["unreviewed"] for i in items):
                note = neutralize(UNREVIEWED_NOTE)
            else:
                note = "" if items else "no recorded guardrail matches this approach"
            return {"items": items, "prompt": wrap(render(found)) if items else "",
                    "warn": bool(items), "note": note}

    # -- status and lifecycle --------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return self._call(self._status)

    def _status(self) -> Dict[str, Any]:
        self.memory.refresh()
        if self.config.read_only and not self.memory.is_shared:
            # loaded, not opened: what open() would have found, read from the files
            real = os.path.realpath(self.config.store_path) if self.config.store_path else ""
            shared = bool(real) and os.path.exists(real + ".journal")
            unfolded = int(getattr(self.memory.graph, "replay_unfolded", 0))
        else:
            shared = bool(self.memory.is_shared)
            unfolded = self.memory.unfolded_transactions()
        counts: Dict[str, int] = {}
        last = ""
        pending = expired = 0
        for kind in KINDS:
            for node in self.memory.graph.nodes(kind):
                record = MemoryRecord.from_node(node)
                if record.status == "active":
                    counts[kind] = counts.get(kind, 0) + 1
                    last = max(last, record.updated_at or record.created_at or "")
                elif record.status == "pending":
                    if pending_expired(record.created_at):
                        expired += 1
                    else:
                        pending += 1
        return {"store": self.config.store_path, "store_exists": self._store_exists,
                "repo_root": self.config.repo_root,
                "agent": self.config.agent, "shared": shared,
                "session": self._session, "counts": counts,
                "total_active": sum(counts.values()), "last_write": last,
                "unfolded_transactions": unfolded,
                "pending": pending, "pending_expired": expired,
                "warnings": self.memory.warnings, "schema": 1}

    def close(self) -> None:
        """Leave a complete snapshot behind and release the store (only if this service opened
        it). A read-only service writes nothing here: it never opened the store, so there is
        nothing to fold or release. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True                 # later calls fail with "service is closed"
            if not self._owns or self.config.read_only:
                return

            def shut() -> None:
                # Every write is already durable in the journal: a checkpoint (here or in
                # close()) that cannot get the lock in time is skipped, as JournalBackend.close()
                # does, never reported as a failure of the call that came before.
                try:
                    if self.memory.unfolded_transactions():
                        self.memory.checkpoint()
                except (StoreLockTimeout, StoreInUse):
                    pass
                finally:
                    try:
                        self.memory.close()
                    except (StoreLockTimeout, StoreInUse):
                        pass

            self._translate(shut)
