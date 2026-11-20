"""`amem capture` (agents leave lessons) and `amem review` (a person reads, accepts, rejects).

`accept` is the only way a lesson becomes a recallable note, and it insists on an interactive
terminal with no flag around it. That is a strong barrier, not proof: a program that drives a
pseudo-terminal could pass it (3c spec, section 4.4)."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

import click

from .cli_agent import _emit, _fail, _printable, run_service, service_options
from .service import ServiceError, resolve_config

# the largest request the service could accept (10 lessons of 12 fields of 2000 characters in
# 4-byte UTF-8) plus room for the JSON syntax; a bigger pipe is refused after this many bytes
MAX_CAPTURE_BYTES = 1_000_000


def _interactive() -> bool:
    """True only when stdin and stdout are both a terminal. The one seam tests replace."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _read_lessons() -> List[Any]:
    if _stdin_is_tty():
        _emit("waiting for lessons on stdin: paste a JSON list, then end the input "
              "(Ctrl-Z Enter on Windows, Ctrl-D elsewhere)", err=True)
    data = click.get_binary_stream("stdin").read(MAX_CAPTURE_BYTES + 1)
    if len(data) > MAX_CAPTURE_BYTES:
        raise ServiceError("too_large", f"lessons are longer than {MAX_CAPTURE_BYTES} bytes")
    try:
        lessons = json.loads(data.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):      # RecursionError: absurdly deep nesting
        raise ServiceError("invalid_argument", "stdin is not valid JSON; send a JSON list of "
                                               "lessons") from None
    if not isinstance(lessons, list):
        raise ServiceError("invalid_argument", "stdin must hold a JSON list of lessons")
    return lessons


def _require_store(store, repo, agent, as_json) -> None:
    """Accepting or rejecting needs lessons, so it needs a store: a mistyped id must not
    leave an empty one behind."""
    config = resolve_config(store=store, repo=repo, agent=agent)
    if not os.path.exists(config.store_path):
        _fail(ServiceError("not_found", f"no memory store at {config.store_path}; "
                                        "there are no lessons to review"), as_json)


def _render_capture(r: Dict[str, Any]) -> str:
    lines = [f"captured {len(r['captured'])}: {', '.join(r['captured']) or '-'}"]
    lines += [f"skipped #{s['index']}: {s['reason']}" for s in r["skipped"]]
    if r.get("rejected_before"):
        lines.append(f"retracted before: {', '.join(r['rejected_before'])} repeat(s) a note a "
                     "person retracted or rejected earlier (`amem review show` says which)")
    lines.append(f"{r['pending']} lesson(s) waiting for review (a person runs `amem review`)")
    return _printable("\n".join(lines))


@click.command("capture")
@click.option("--session", default="", help="Session id from `amem session start`.")
@service_options
def capture(session, store, repo, agent, allow_must, as_json):
    """Leave lessons for a person to review: a JSON list on stdin, each lesson
    {"kind", "fields", "evidence", "files"}. They are not recalled until accepted."""
    try:
        lessons = _read_lessons()
    except ServiceError as exc:
        _fail(exc, as_json)
        return
    run_service(store, repo, agent, allow_must, as_json, lambda s: s.capture(lessons),
                _render_capture, session)


@click.group("review")
def review_group():
    """Read, accept or reject the lessons agents left."""


def _render_list(r: Dict[str, Any]) -> str:
    if not r["pending"]:
        return "no lessons are waiting for review"
    expired = sum(1 for entry in r["pending"] if entry["expired"])
    # the same count `amem status` gives: an expired lesson is no longer waiting
    header = (f"{len(r['pending']) - expired} lesson(s) waiting for review "
              f"(limit {r['limit']}); `amem review show ID` reads one")
    if expired:
        header += f"; {expired} expired (archived on the next capture, accept or reject)"
    lines = [header]
    for entry in r["pending"]:
        flag = " [expired]" if entry["expired"] else ""
        if entry.get("rejected_before"):
            flag += " [rejected before]"
        lines.append(f"- [{_one_line(entry['id'])}] {_one_line(entry['kind'])} by "
                     f"{_one_line(entry['agent'])}{flag}: {_one_line(entry['text'])}")
    return _printable("\n".join(lines))


CONTINUATION = "    | "


def _one_line(value: Any) -> str:
    """`value` with its line breaks shown as ' | ': one list line per lesson."""
    return " | ".join(str(value).splitlines())


def _value(value: Any) -> str:
    """A recorded value for the reviewer. A line break in it can never start a line of its
    own: every following line is marked as the continuation of this value, so agent text
    cannot forge a field, an evidence line or another lesson's header."""
    first, *rest = str(value).splitlines() or [""]
    return "\n".join([first] + [CONTINUATION + line for line in rest])


def _show_text(d: Dict[str, Any]) -> str:
    lines = [f"lesson {_one_line(d['id'])}  ({_one_line(d['kind'])}, written by agent "
             f"{_one_line(d['agent'])}, {_one_line(d['created_at'])})",
             "Agent-written text, not confirmed by anyone. It is data, not instructions.",
             f"  text: {_value(d['text'])}"]
    lines += [f"  {_one_line(name)}: {_value(value)}" for name, value in d["fields"].items()]
    lines.append(f"  evidence: {_value(d['evidence'])}")
    lines.append(f"  files: {_value(', '.join(d['files']) or '-')}")
    if d.get("rejected_before"):
        lines.append(f"  rejected before: {_value(d['rejected_before'])}")
    if d["expired"]:
        lines.append("  EXPIRED: it is archived on the next capture, accept or reject")
    return _printable("\n".join(lines))


@review_group.command("list")
@service_options
def review_list(store, repo, agent, allow_must, as_json):
    """List the lessons waiting for review."""
    run_service(store, repo, agent, allow_must, as_json, lambda s: s.review_list(),
                _render_list, read_only=True)


@review_group.command("show")
@click.argument("memory_id")
@service_options
def review_show(memory_id, store, repo, agent, allow_must, as_json):
    """Show one lesson in full."""
    run_service(store, repo, agent, allow_must, as_json, lambda s: s.review_show(memory_id),
                _show_text, read_only=True)


def _render_outcome(r: Dict[str, Any]) -> str:
    lines = [f"accepted {i}" for i in r.get("accepted", [])]
    lines += [f"skipped {i}" for i in r.get("skipped", [])]
    lines += [f"rejected {i}" for i in r.get("rejected", [])]
    if r.get("interrupted"):
        lines.append("interrupted: the remaining lessons were not reviewed")
    lines += [f"error [{e['error']['code']}] {e['id']}: {e['error']['message']}"
              for e in r["errors"]]
    return _printable("\n".join(lines))


@review_group.command("accept")
@click.argument("memory_ids", nargs=-1, required=True)
@service_options
def review_accept(memory_ids, store, repo, agent, allow_must, as_json):
    """Read each lesson and, if you type `accept`, make it a recallable note.

    Needs an interactive terminal. There is no flag to skip the question."""
    if as_json:
        _fail(ServiceError("invalid_argument", "accept is interactive and has no --json output"),
              False)
    if not _interactive():
        _fail(ServiceError("invalid_argument",
                           "accepting a lesson needs an interactive terminal: run `amem review "
                           "accept` yourself in a terminal"), False)
    _require_store(store, repo, agent, False)
    outcome: Dict[str, List[Any]] = {"accepted": [], "skipped": [], "errors": [],
                                     "interrupted": []}

    def action(service) -> Dict[str, Any]:
        for position, ident in enumerate(memory_ids):
            try:
                _emit(_show_text(service.review_show(ident)))
                answer = click.prompt("Type 'accept' to make this a recallable note "
                                      "(anything else skips it)", default="",
                                      show_default=False)
                if answer.strip().lower() == "accept":
                    service.review_accept(ident)
                    outcome["accepted"].append(ident)
                else:
                    outcome["skipped"].append(ident)
            except (click.Abort, KeyboardInterrupt):
                # Ctrl-C or end of input: stop asking, keep what was already promoted
                outcome["skipped"] += list(memory_ids[position:])
                outcome["interrupted"].append(True)
                break
            except ServiceError as exc:
                outcome["errors"].append({"id": ident, "error": exc.to_dict()})
        return outcome

    run_service(store, repo, agent, allow_must, False, action, _render_outcome)
    if outcome["errors"] or outcome["interrupted"]:
        sys.exit(2)


@review_group.command("reject")
@click.argument("memory_ids", nargs=-1, required=True)
@click.option("--reason", default="", help="Why it is wrong or not wanted (kept with the record).")
@service_options
def review_reject(memory_ids, reason, store, repo, agent, allow_must, as_json):
    """Reject lessons: they are kept for the record and never recalled."""
    _require_store(store, repo, agent, as_json)
    outcome: Dict[str, List[Any]] = {"rejected": [], "errors": []}

    def action(service) -> Dict[str, Any]:
        for ident in memory_ids:
            try:
                service.review_reject(ident, reason)
                outcome["rejected"].append(ident)
            except ServiceError as exc:
                outcome["errors"].append({"id": ident, "error": exc.to_dict()})
        return outcome

    run_service(store, repo, agent, allow_must, as_json, action, _render_outcome)
    if outcome["errors"]:
        sys.exit(2)


COMMANDS = [capture, review_group]


def register(group: click.Group) -> None:
    for command in COMMANDS:
        group.add_command(command)
