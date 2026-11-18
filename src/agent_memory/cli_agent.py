"""Agent-facing commands (added to the `amem` group by `register`, called from cli.py).

Never imports `mcp` at import time: only `serve` loads the MCP server, lazily."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
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
    try:
        run_server(resolve_config(store=store, repo=repo, agent=agent, allow_must=allow_must))
    except ServiceError as exc:                  # e.g. a store the service refuses at startup
        _fail(exc, False)


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


# C0 controls except tab and newline, DEL, and C1 controls: what a terminal could act on.
_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _printable(text: str) -> str:
    """`text` without terminal control characters (an ESC or CSI sequence in a stored id or a
    warning must never reach the terminal); tabs and newlines are kept."""
    return _CONTROLS.sub("", text)


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
            except BaseException:
                try:
                    service.close()
                except Exception:
                    pass                     # the command's own error is the one to report
                raise
            try:
                service.close()
            except Exception:
                # the result is computed and its writes are durable: report it, not this (a
                # retry would repeat the command: a second session, feedback counted twice)
                _emit("note: the store could not be closed cleanly; the command's result is "
                      "already saved", err=True)
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
                lambda r: _printable(
                    f"{'created' if r['created'] else 'merged into existing'} {r['id']}"
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
        return _printable("\n".join(lines))
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


def _install_fail(message: str, code: str, as_json: bool, exit_code: int) -> None:
    if as_json:
        click.echo(json.dumps({"error": {"code": code, "message": message}}, indent=2))
    else:
        _emit(_printable(f"error: {message}"), err=True)
    sys.exit(exit_code)


def _shown_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


@click.command("install")
@click.argument("tool")
@click.option("--repo", default=None, help="Repository root (default: the git top-level).")
@click.option("--store", default=None, help="Pin the store path in the server command "
              "(default: let the server find it, which keeps the config portable).")
@click.option("--command", "server_command", default="amem",
              help="The command that starts the server.")
@click.option("--write", is_flag=True, help="Apply the changes (default: show them).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def install(tool, repo, store, server_command, write, as_json):
    """Wire this repository to TOOL (claude-code, cursor, codex, devin, generic): MCP config
    and a marked instruction block.

    Shows a diff and changes nothing unless --write is given. Content outside the marked
    block is never touched."""
    from .installer import InstallError
    from .installer import plan as planner
    from .installer.changes import apply_changes
    from .installer.tools import TOOLS
    from .service import find_repo_root
    if tool not in TOOLS:
        _install_fail(f"unknown tool {tool!r}; choose one of: {', '.join(sorted(TOOLS))}",
                      "invalid_argument", as_json, 2)
    try:
        root = Path(repo or find_repo_root()).resolve()
        plan = planner.plan_install(tool, root, store=store, command=server_command)
        pending = [c for c in plan.changes if c.status != "unchanged"]
        if write:
            apply_changes(plan.changes, root=str(root))
    except InstallError as exc:
        _install_fail(str(exc), "install_failed", as_json, 1)
    except Exception:
        # never a traceback, and no path but the files the install was asked to change
        _install_fail("the install failed unexpectedly", "internal", as_json, 1)
    if as_json:
        click.echo(json.dumps({
            "tool": tool, "written": bool(write and pending),
            "changes": [{"path": str(c.path), "status": c.status, "note": c.note}
                        for c in plan.changes],
            "commands": plan.commands, "notes": plan.notes}, indent=2))
        return
    out = []
    if not pending:
        out.append(f"Nothing to change: this repository is already set up for {tool}.")
    for change in pending:
        out.append(f"{change.status}: {_shown_path(change.path, root)}  ({change.note})")
        out.append(change.diff())
    for line in plan.commands:
        out.append(f"Run this to register the server: {line}")
    for note in plan.notes:
        out.append(f"note: {note}")
    if pending:
        out.append(f"Wrote {len(pending)} file(s)." if write
                   else "Dry run: nothing was written. Re-run with --write to apply.")
    _emit(_printable("\n".join(out)))


@click.command("doctor")
@click.option("--repo", default=None, help="Repository root (default: the git top-level).")
@click.option("--store", default=None, help="Memory store path "
              "(default: <repo>/.agent-memory/project.memory).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def doctor(repo, store, as_json):
    """Check the memory store and which agent tools are wired up (exit 1 if anything is wrong)."""
    from . import doctor as diagnose
    try:
        # the store and repo the other commands would use: arguments, then the environment
        config = resolve_config(store=store, repo=repo)
        checks = diagnose.run_doctor(config.repo_root, config.store_path)
    except Exception:
        # never a traceback, a path or anything else read from the failure
        checks = [{"check": "doctor", "ok": False, "detail": "the doctor failed unexpectedly"}]
    ok = all(c["ok"] for c in checks)
    if as_json:
        click.echo(json.dumps({"ok": ok, "checks": checks}, indent=2))
    else:
        _emit(_printable("\n".join(f"{'ok  ' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}"
                                   for c in checks)))
    sys.exit(0 if ok else 1)


COMMANDS = [serve, recall, guard, remember, feedback, retract, verify, status, session_group,
            install, doctor]


def register(group: click.Group) -> None:
    for command in COMMANDS:
        group.add_command(command)
