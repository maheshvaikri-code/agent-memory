"""MCP stdio server over MemoryService. Needs the optional `mcp` package (Python 3.10+):
pip install 'agent-memory[mcp]'.

The module prints nothing: stdout carries only protocol frames."""
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from mcp.server.fastmcp import Context, FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
except ImportError as exc:                                       # pragma: no cover
    raise ImportError("The MCP server needs the optional 'mcp' package (Python 3.10+): "
                      "pip install 'agent-memory[mcp]'") from exc

from .protocol import block
from .service import MemoryService, ServiceConfig, ServiceError

TOOL_METHODS = {
    "memory_recall": "recall", "memory_check": "check", "memory_remember": "remember",
    "memory_feedback": "feedback", "memory_supersede": "supersede", "memory_retract": "retract",
    "memory_verify": "verify", "memory_session_start": "session_start",
    "memory_session_end": "session_end", "memory_capture_lessons": "capture",
    "memory_status": "status",
}
TOOL_NAMES = tuple(TOOL_METHODS)
# What a read-only caller may reach. Every other tool refuses with `forbidden` before the
# service is touched; tests pin that every tool is one or the other.
READ_ONLY_TOOLS = frozenset(("memory_recall", "memory_check", "memory_status"))

_log = logging.getLogger(__name__)


def _client_name(ctx: Context) -> Optional[str]:
    try:
        params = ctx.session.client_params
        return params.clientInfo.name if params and params.clientInfo else None
    except Exception:
        return None


def _tool_error(code: str, message: str) -> ToolError:
    return ToolError(json.dumps({"error": {"code": code, "message": message}}))


# Every text parameter is annotated exactly `str` (optional ones default to ""): FastMCP
# JSON-parses a string argument whose annotation is anything else (Optional[str] included), so
# a context or evidence that happens to look like JSON would be rejected or turned into None.
def build_server(service: Optional[MemoryService] = None, *,
                 provider: Optional[Callable[[], Tuple[Any, str]]] = None,
                 trust_client_name: Optional[bool] = None,
                 observer: Optional[Callable[[str, str, int], None]] = None,
                 scrub: Optional[Callable[[str], str]] = None,
                 settings: Optional[Dict[str, Any]] = None) -> FastMCP:
    """`service` serves stdio: one service, read-write, the client's own name stamped on writes.
    A `provider` serves many callers: it returns (service, role) for the current call. The
    client-reported name is trusted only when `trust_client_name` says so; left unset it is
    trusted for a plain `service` and ignored for a `provider`. `observer` hears every call's
    outcome and `scrub` cleans every error message that leaves."""
    if (service is None) == (provider is None):
        raise ValueError("pass either a service or a provider")
    trust = (service is not None) if trust_client_name is None else trust_client_name
    if provider is None:
        provider = lambda: (service, "read-write")           # noqa: E731
    app = FastMCP("agent-memory", instructions=block("mcp"), **(settings or {}))

    def run(ctx: Context, tool: str, **kwargs: Any) -> Dict[str, Any]:
        started, outcome = time.monotonic(), "ok"
        try:
            target, role = provider()
            if role != "read-write" and tool not in READ_ONLY_TOOLS:
                raise ServiceError("forbidden", "this token is read-only: it can recall, check "
                                                "and read the status, not change the memory")
            if trust:
                target.set_client(_client_name(ctx))
            return getattr(target, TOOL_METHODS[tool])(**kwargs)
        except ServiceError as exc:
            outcome = exc.code
            if exc.code == "internal":         # the client gets only the generic message
                _log.error("internal failure in an agent-memory tool: %s", exc.message,
                           exc_info=exc.__cause__ or exc)
            raise _tool_error(exc.code, scrub(exc.message) if scrub else exc.message) from None
        except Exception:
            outcome = "internal"
            # never reaches the client: no traceback, no path; the operator sees it on stderr
            _log.exception("unexpected failure in an agent-memory tool")
            raise _tool_error("internal", "the memory server failed unexpectedly") from None
        finally:
            if observer is not None:
                try:
                    observer(tool, outcome, int((time.monotonic() - started) * 1000))
                except Exception:
                    _log.exception("the call observer failed")

    @app.tool(description="Recall recorded project memory relevant to a task. Call at the start "
                          "of a task with the task and the files you expect to touch. Optional "
                          "context: text you already have (error output, a diff hunk, your goal) "
                          "to rank notes by. Returns ranked notes with why each matched; empty "
                          "when nothing is relevant.")
    def memory_recall(task: str, ctx: Context, files: Optional[List[str]] = None,
                      symbols: Optional[List[str]] = None,
                      max_tokens: Optional[int] = None,
                      context: str = "") -> Dict[str, Any]:
        return run(ctx, "memory_recall", task=task, files=files, symbols=symbols,
                   max_tokens=max_tokens, context=context or None)

    @app.tool(description="Check a proposed approach against recorded failures: rejected "
                          "alternatives, bug patterns, failed refactors, constraints. Call "
                          "before adopting an approach in an area with past problems. "
                          "`warn` is true when something matches. Items marked `unreviewed` are "
                          "agent notes no person has confirmed: verify before acting.")
    def memory_check(approach: str, ctx: Context,
                     files: Optional[List[str]] = None) -> Dict[str, Any]:
        return run(ctx, "memory_check", approach=approach, files=files)

    @app.tool(description="Record one fact. kind: convention{category,rule} decision{title,context} "
                          "bug_pattern{name,signature} alternative{option,rejected_because} "
                          "quirk{description} workaround{solution,description} "
                          "refactoring_attempt{outcome,approach,reason_failed} "
                          "open_question{question} blocker{description} "
                          "partial_work{work_type,location,state}. files anchors the note to "
                          "repo files so it is flagged when they change.")
    def memory_remember(kind: str, fields: Dict[str, Any], ctx: Context,
                        files: Optional[List[str]] = None, evidence: str = "",
                        confidence: Optional[float] = None,
                        priority: str = "") -> Dict[str, Any]:
        return run(ctx, "memory_remember", kind=kind, fields=fields, files=files,
                   evidence=evidence, confidence=confidence, priority=priority or None)

    @app.tool(description="Tell the memory whether a recalled note helped (true) or misled "
                          "(false), by its id.")
    def memory_feedback(id: str, helpful: bool, ctx: Context) -> Dict[str, Any]:
        return run(ctx, "memory_feedback", id=id, helpful=helpful)

    @app.tool(description="Replace a recorded note with a corrected one of the same shape as "
                          "memory_remember. The old note is kept but no longer returned.")
    def memory_supersede(old_id: str, kind: str, fields: Dict[str, Any], ctx: Context,
                         files: Optional[List[str]] = None,
                         evidence: str = "") -> Dict[str, Any]:
        return run(ctx, "memory_supersede", old_id=old_id, kind=kind, fields=fields, files=files,
                   evidence=evidence)

    @app.tool(description="Mark a note as wrong. It is kept for the record and no longer returned.")
    def memory_retract(id: str, ctx: Context, reason: str = "") -> Dict[str, Any]:
        return run(ctx, "memory_retract", id=id, reason=reason)

    @app.tool(description="Confirm a note still holds after you re-read the code it is anchored "
                          "to: re-stamps its file anchors so it stops being flagged as changed.")
    def memory_verify(id: str, ctx: Context) -> Dict[str, Any]:
        return run(ctx, "memory_verify", id=id)

    @app.tool(description="Start a session for this task. Returns open items (questions, "
                          "blockers, partial work) left by an earlier session that was never "
                          "ended. Call once at the start of a task.")
    def memory_session_start(task: str, ctx: Context) -> Dict[str, Any]:
        return run(ctx, "memory_session_start", task=task)

    @app.tool(description="End the current session. Optional one-line summary and optional "
                          "lessons (as in memory_capture_lessons). Returns the items still "
                          "unresolved and, when lessons were given, the capture result.")
    def memory_session_end(ctx: Context, summary: str = "", completion: float = 1.0,
                           lessons: Optional[List[Any]] = None) -> Dict[str, Any]:
        return run(ctx, "memory_session_end", summary=summary, completion=completion,
                   lessons=lessons)

    @app.tool(description="Hand over lessons from this session for a person to review. Each "
                          "lesson: {kind, fields, evidence, files, symbols}; kind and fields "
                          "as in memory_remember; evidence and at least one file or symbol "
                          "are required. Lessons are NOT recalled until a person accepts "
                          "them. Up to 10 per call. Returns captured ids and skipped "
                          "lessons with reasons.")
    def memory_capture_lessons(lessons: List[Any], ctx: Context) -> Dict[str, Any]:
        return run(ctx, "memory_capture_lessons", lessons=lessons)

    @app.tool(description="Memory status: store, your agent name, counts by kind, last write, "
                          "storage warnings.")
    def memory_status(ctx: Context) -> Dict[str, Any]:
        return run(ctx, "memory_status")

    return app


def serve(config: ServiceConfig) -> None:
    """Serve over stdio until the client disconnects, then leave a complete snapshot."""
    service = MemoryService(config)
    try:
        build_server(service).run("stdio")
    finally:
        service.close()
