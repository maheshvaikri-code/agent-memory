"""MCP stdio server over MemoryService. Needs the optional `mcp` package (Python 3.10+):
pip install 'agent-memory[mcp]'.

The module prints nothing: stdout carries only protocol frames."""
import json
import logging
from typing import Any, Callable, Dict, List, Optional

try:
    from mcp.server.fastmcp import Context, FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
except ImportError as exc:                                       # pragma: no cover
    raise ImportError("The MCP server needs the optional 'mcp' package (Python 3.10+): "
                      "pip install 'agent-memory[mcp]'") from exc

from .protocol import block
from .service import MemoryService, ServiceConfig, ServiceError

TOOL_NAMES = ("memory_recall", "memory_check", "memory_remember", "memory_feedback",
              "memory_supersede", "memory_retract", "memory_verify", "memory_session_start",
              "memory_session_end", "memory_status")

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
def build_server(service: MemoryService) -> FastMCP:
    app = FastMCP("agent-memory", instructions=block("mcp"))

    def run(ctx: Context, fn: Callable[..., Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        try:
            service.set_client(_client_name(ctx))
            return fn(**kwargs)
        except ServiceError as exc:
            if exc.code == "internal":         # the client gets only the generic message
                _log.error("internal failure in an agent-memory tool: %s", exc.message,
                           exc_info=exc.__cause__ or exc)
            raise _tool_error(exc.code, exc.message) from None
        except Exception:
            # never reaches the client: no traceback, no path; the operator sees it on stderr
            _log.exception("unexpected failure in an agent-memory tool")
            raise _tool_error("internal", "the memory server failed unexpectedly") from None

    @app.tool(description="Recall recorded project memory relevant to a task. Call at the start "
                          "of a task with the task and the files you expect to touch. Optional "
                          "context: text you already have (error output, a diff hunk, your goal) "
                          "to rank notes by. Returns ranked notes with why each matched; empty "
                          "when nothing is relevant.")
    def memory_recall(task: str, ctx: Context, files: Optional[List[str]] = None,
                      symbols: Optional[List[str]] = None,
                      max_tokens: Optional[int] = None,
                      context: str = "") -> Dict[str, Any]:
        return run(ctx, service.recall, task=task, files=files, symbols=symbols,
                   max_tokens=max_tokens, context=context or None)

    @app.tool(description="Check a proposed approach against recorded failures: rejected "
                          "alternatives, bug patterns, failed refactors, constraints. Call "
                          "before adopting an approach in an area with past problems. "
                          "`warn` is true when something matches. Items marked `unreviewed` are "
                          "agent notes no person has confirmed: verify before acting.")
    def memory_check(approach: str, ctx: Context,
                     files: Optional[List[str]] = None) -> Dict[str, Any]:
        return run(ctx, service.check, approach=approach, files=files)

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
        return run(ctx, service.remember, kind=kind, fields=fields, files=files,
                   evidence=evidence, confidence=confidence, priority=priority or None)

    @app.tool(description="Tell the memory whether a recalled note helped (true) or misled "
                          "(false), by its id.")
    def memory_feedback(id: str, helpful: bool, ctx: Context) -> Dict[str, Any]:
        return run(ctx, service.feedback, id=id, helpful=helpful)

    @app.tool(description="Replace a recorded note with a corrected one of the same shape as "
                          "memory_remember. The old note is kept but no longer returned.")
    def memory_supersede(old_id: str, kind: str, fields: Dict[str, Any], ctx: Context,
                         files: Optional[List[str]] = None,
                         evidence: str = "") -> Dict[str, Any]:
        return run(ctx, service.supersede, old_id=old_id, kind=kind, fields=fields, files=files,
                   evidence=evidence)

    @app.tool(description="Mark a note as wrong. It is kept for the record and no longer returned.")
    def memory_retract(id: str, ctx: Context, reason: str = "") -> Dict[str, Any]:
        return run(ctx, service.retract, id=id, reason=reason)

    @app.tool(description="Confirm a note still holds after you re-read the code it is anchored "
                          "to: re-stamps its file anchors so it stops being flagged as changed.")
    def memory_verify(id: str, ctx: Context) -> Dict[str, Any]:
        return run(ctx, service.verify, id=id)

    @app.tool(description="Start a session for this task. Returns open items (questions, "
                          "blockers, partial work) left by an earlier session that was never "
                          "ended. Call once at the start of a task.")
    def memory_session_start(task: str, ctx: Context) -> Dict[str, Any]:
        return run(ctx, service.session_start, task=task)

    @app.tool(description="End the current session. Optional one-line summary. Returns the "
                          "items still unresolved so you can see what is left.")
    def memory_session_end(ctx: Context, summary: str = "",
                           completion: float = 1.0) -> Dict[str, Any]:
        return run(ctx, service.session_end, summary=summary, completion=completion)

    @app.tool(description="Memory status: store, your agent name, counts by kind, last write, "
                          "storage warnings.")
    def memory_status(ctx: Context) -> Dict[str, Any]:
        return run(ctx, service.status)

    return app


def serve(config: ServiceConfig) -> None:
    """Serve over stdio until the client disconnects, then leave a complete snapshot."""
    service = MemoryService(config)
    try:
        build_server(service).run("stdio")
    finally:
        service.close()
