"""Agent-facing commands (added to the `amem` group by `register`, called from cli.py).

Never imports `mcp` at import time: only `serve` loads the MCP server, lazily."""
from __future__ import annotations

import json
import sys
from typing import Callable, Dict, Optional, Tuple

import click

from .service import MemoryService, ServiceError, resolve_config
from .service.service import MAX_CONTEXT_CHARS

# `--context -` reads at most this many bytes of stdin: the longest context the service
# accepts in 4-byte UTF-8 characters, plus a little room for whitespace it strips.
MAX_CONTEXT_BYTES = MAX_CONTEXT_CHARS * 4 + 8


@click.command("serve")
@click.option("--store", default=None, help="Memory store path.")
@click.option("--repo", default=None, help="Repository root.")
@click.option("--agent", default=None, help="Default agent name (an MCP client's own name wins).")
@click.option("--allow-must", is_flag=True, help="Let agents record priority 'must' notes.")
def serve(store, repo, agent, allow_must):
    """Serve the memory over MCP (stdio). Started by an MCP client, not by hand."""
    try:
        from .mcp_server import serve as run_server
    except ImportError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    run_server(resolve_config(store=store, repo=repo, agent=agent, allow_must=allow_must))


def service_options(fn):
    for option in reversed([
        click.option("--store", default=None, help="Memory store path "
                     "(default: <repo>/.agent-memory/project.memory)."),
        click.option("--repo", default=None, help="Repository root (default: the git top-level)."),
        click.option("--agent", default=None, help="Agent name recorded on writes."),
        click.option("--allow-must", is_flag=True, help="Allow priority 'must' notes."),
        click.option("--json", "as_json", is_flag=True, help="Machine-readable output."),
    ]):
        fn = option(fn)
    return fn


def _emit(text: str, err: bool = False) -> None:
    """Write human-readable text without ever failing on a character the stream cannot encode
    (a cp1252 pipe on Windows meeting a recalled note's arrow, U+2192): such characters become
    '?'. JSON output is ASCII (json.dumps escapes the rest), so it never needs this."""
    stream = click.get_text_stream("stderr" if err else "stdout")
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except LookupError:                      # an unknown codec name: keep plain ASCII
        text = text.encode("ascii", errors="replace").decode("ascii")
    click.echo(text, err=err)


def _fail(exc: ServiceError, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps({"error": exc.to_dict()}, indent=2))
    else:
        _emit(f"error [{exc.code}]: {exc.message}", err=True)
    sys.exit(2)


def run_service(store, repo, agent, allow_must, as_json, action: Callable[[MemoryService], dict],
                render: Callable[[dict], str], session: str = ""):
    try:
        try:
            service = MemoryService(resolve_config(store=store, repo=repo, agent=agent,
                                                   allow_must=allow_must))
            try:
                if session:
                    service.use_session(session)
                result = action(service)
            finally:
                service.close()
            if as_json:
                click.echo(json.dumps(result, indent=2))
            else:
                _emit(render(result))
        except ServiceError:
            raise
        except Exception:
            # never a traceback or a filesystem path: the same generic error the service gives
            raise ServiceError("internal", "the memory command failed unexpectedly") from None
    except ServiceError as exc:
        _fail(exc, as_json)


def parse_fields(pairs: Tuple[str, ...]) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise click.BadParameter(f"{pair!r} must be written key=value", param_hint="-F")
        if key.strip() in fields:
            raise click.BadParameter(f"field {key.strip()!r} is given more than once",
                                     param_hint="-F")
        fields[key.strip()] = value
    return fields


def _notes(result: dict) -> str:
    """The prompt as the service shaped it (already neutralised: never re-rendered here). A note
    beside a prompt (relevant notes dropped for the budget) goes to stderr; with no prompt the
    note itself is the output."""
    if result["prompt"] and result["note"]:
        _emit(result["note"], err=True)
    return result["prompt"] or result["note"]


def _read_context(context: Optional[str]) -> Optional[str]:
    """`--context -` reads the text from stdin; stdin is never touched otherwise. The read is
    bounded (a megabyte pipe is refused after MAX_CONTEXT_BYTES, not read whole) and undecodable
    bytes become U+FFFD; the service then applies its own character limit."""
    if context != "-":
        return context
    data = click.get_binary_stream("stdin").read(MAX_CONTEXT_BYTES + 1)
    if len(data) > MAX_CONTEXT_BYTES:
        raise ServiceError("too_large", f"context is longer than {MAX_CONTEXT_CHARS} characters")
    return data.decode("utf-8", errors="replace")


@click.command("recall")
@click.argument("task")
@click.option("-f", "--file", "files", multiple=True, help="A file you expect to touch.")
@click.option("--symbol", "symbols", multiple=True)
@click.option("--max-tokens", type=int, default=None)
@click.option("--context", default=None,
              help="Text you already have (error output, a diff hunk, your goal) to rank notes "
                   "by; '-' reads it from stdin.")
@click.option("--session", default="", help="Session id from `amem session start`.")
@service_options
def recall(task, files, symbols, max_tokens, context, session, store, repo, agent, allow_must,
           as_json):
    """Recall recorded memory relevant to TASK."""
    run_service(store, repo, agent, allow_must, as_json,
                lambda s: s.recall(task, list(files), list(symbols), max_tokens,
                                   _read_context(context)),
                _notes, session)


@click.command("guard")
@click.argument("approach")
@click.option("-f", "--file", "files", multiple=True)
@click.option("--session", default="")
@service_options
def guard(approach, files, session, store, repo, agent, allow_must, as_json):
    """Check APPROACH against recorded failures (rejected alternatives, bug patterns)."""
    run_service(store, repo, agent, allow_must, as_json,
                lambda s: s.check(approach, list(files)), _notes, session)


@click.command("remember")
@click.argument("kind")
@click.option("-F", "--field", "pairs", multiple=True, help="key=value; repeat per field.")
@click.option("-f", "--file", "files", multiple=True, help="Anchor the note to a repo file.")
@click.option("--evidence", default="")
@click.option("--confidence", type=float, default=None)
@click.option("--priority", default=None)
@click.option("--session", default="")
@service_options
def remember(kind, pairs, files, evidence, confidence, priority, session, store, repo, agent,
             allow_must, as_json):
    """Record one fact of KIND (convention, decision, bug_pattern, ...)."""
    fields = parse_fields(pairs)
    run_service(store, repo, agent, allow_must, as_json,
                lambda s: s.remember(kind, fields, list(files), evidence, confidence, priority),
                lambda r: (f"{'created' if r['created'] else 'merged into existing'} {r['id']}"
                           + (f" (similar: {', '.join(r['similar'])})" if r["similar"] else "")),
                session)


@click.command("feedback")
@click.argument("memory_id")
@click.option("--helpful/--harmful", default=True)
@click.option("--session", default="")
@service_options
def feedback(memory_id, helpful, session, store, repo, agent, allow_must, as_json):
    """Say whether a recalled note helped or misled."""
    run_service(store, repo, agent, allow_must, as_json,
                lambda s: s.feedback(memory_id, helpful), lambda r: f"recorded for {r['id']}", session)


@click.command("retract")
@click.argument("memory_id")
@click.option("--reason", default="")
@click.option("--session", default="")
@service_options
def retract(memory_id, reason, session, store, repo, agent, allow_must, as_json):
    """Mark a note as wrong (kept, no longer returned)."""
    run_service(store, repo, agent, allow_must, as_json,
                lambda s: s.retract(memory_id, reason), lambda r: f"retracted {r['id']}", session)


@click.command("verify")
@click.argument("memory_id")
@service_options
def verify(memory_id, store, repo, agent, allow_must, as_json):
    """Re-confirm a note against the current files it is anchored to."""
    run_service(store, repo, agent, allow_must, as_json,
                lambda s: s.verify(memory_id), lambda r: f"{r['restamped']} anchor(s) re-stamped")


@click.command("status")
@service_options
def status(store, repo, agent, allow_must, as_json):
    """Show the store, counts by kind and any storage warnings."""
    def render(r):
        lines = [f"store: {r['store']}", f"repo: {r['repo_root']}",
                 f"active notes: {r['total_active']}  last write: {r['last_write'] or '-'}"]
        lines += [f"  {kind}: {n}" for kind, n in sorted(r["counts"].items())]
        lines += [f"warning: {w}" for w in r["warnings"]]
        return "\n".join(lines)
    run_service(store, repo, agent, allow_must, as_json, lambda s: s.status(), render)


@click.group("session")
def session_group():
    """Start and end a work session."""


@session_group.command("start")
@click.argument("task")
@service_options
def session_start(task, store, repo, agent, allow_must, as_json):
    """Start a session; prints its id (pass it to later commands with --session)."""
    def render(r):
        lines = [f"session {r['session_id']}"]
        lines += [f"carried: {i['text']}" for i in r["carried"]]
        return "\n".join(lines)
    run_service(store, repo, agent, allow_must, as_json, lambda s: s.session_start(task), render)


@session_group.command("end")
@click.argument("session_id")
@click.option("--summary", default="")
@click.option("--completion", type=float, default=1.0)
@service_options
def session_end(session_id, summary, completion, store, repo, agent, allow_must, as_json):
    """End SESSION_ID; lists what is still unresolved."""
    def render(r):
        lines = [f"ended {r['session_id']}"]
        lines += [f"unresolved: {i['text']}" for i in r["unresolved"]]
        return "\n".join(lines)
    run_service(store, repo, agent, allow_must, as_json,
                lambda s: s.session_end(summary, completion), render, session=session_id)


COMMANDS = [serve, recall, guard, remember, feedback, retract, verify, status, session_group]


def register(group: click.Group) -> None:
    for command in COMMANDS:
        group.add_command(command)
