"""Plan the changes `amem install TOOL` would make, without making them."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from ..protocol import block
from . import InstallError
from .changes import Change, read_text
from .gitignore import ensure_ignored
from .jsonmerge import merge_mcp_servers
from .markers import apply_block
from .tools import TOOLS

SERVER_NAME = "agent-memory"

# Printed commands are quoted for the shell they will be pasted into.
_WINDOWS = os.name == "nt"
_CONTROL = re.compile("[\x00-\x1f\x7f]")


@dataclass
class InstallPlan:
    tool: str
    changes: List[Change] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _server_args(store: Optional[str]) -> List[str]:
    return ["serve"] + (["--store", store] if store else [])


def _command_line(parts: List[str]) -> str:
    return subprocess.list2cmdline(parts) if _WINDOWS else " ".join(shlex.quote(p) for p in parts)


def _pinned_store(text: Optional[str]) -> Optional[str]:
    """The `--store` value the existing entry for our server passes, if any. Unreadable or oddly
    shaped configs give None (the merge itself reports those)."""
    try:
        data: Any = json.loads((text or "").lstrip("\ufeff") or "{}")
        args = data["mcpServers"][SERVER_NAME]["args"]
    except (ValueError, KeyError, TypeError, IndexError):
        return None
    if not isinstance(args, list):
        return None
    for index, arg in enumerate(args):
        if arg == "--store" and index + 1 < len(args):
            return str(args[index + 1])
        if isinstance(arg, str) and arg.startswith("--store="):
            return arg[len("--store="):]
    return None


def _key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _edit(plan: InstallPlan, path: Path, note: str,
          transform: Callable[[Optional[str]], str]) -> None:
    """Add `transform(current text or None)` to the plan as the change for `path`. A second edit
    to a file already in the plan is folded into that one change (two changes for one file would
    drop the first). A refusal names the file."""
    key = _key(path)
    earlier = next((c for c in plan.changes if _key(c.path) == key), None)
    try:
        if earlier is not None:
            earlier.new = transform(earlier.new)
            earlier.note = f"{earlier.note}; {note}"
            return
        old, newline = read_text(path)
        new = transform(old)
    except InstallError as exc:
        message = str(exc)
        raise InstallError(message if str(path) in message else f"{path}: {message}") from None
    plan.changes.append(Change(path, old, new, note, newline))


def plan_install(tool: str, repo_root, *, store: Optional[str] = None,
                 command: str = "amem") -> InstallPlan:
    if tool not in TOOLS:
        raise InstallError(f"unknown tool {tool!r}; choose one of: {', '.join(sorted(TOOLS))}")
    for option, value in (("--store", store), ("--command", command)):
        if value is not None and _CONTROL.search(value):
            raise InstallError(f"{option} contains a control character; refusing it so the "
                               "printed command and the written config cannot differ")
    spec = TOOLS[tool]
    root = Path(repo_root)
    if not root.is_dir():
        raise InstallError(f"{root} is not a directory")
    plan = InstallPlan(tool)

    # the instruction block
    body = block(spec.mode)
    _edit(plan, root / spec.instruction_path, f"{spec.label} instructions",
          lambda old: apply_block(old if old is not None else spec.front_matter, body))

    # the MCP registration
    args = _server_args(store)
    if spec.mcp_json_path:
        entry = {"command": command, "args": args}

        def register(old: Optional[str]) -> str:
            pinned = _pinned_store(old)
            if pinned is not None and store is None:
                option = _command_line(["--store", pinned])
                plan.notes.append(
                    f"{spec.mcp_json_path}: the existing {SERVER_NAME} entry pinned the store "
                    f"with {option}; this install drops that pin so the server finds the "
                    f"store from the repository. To keep it, re-run with {option}.")
            return merge_mcp_servers(old, SERVER_NAME, entry)

        _edit(plan, root / spec.mcp_json_path, f"{spec.label} MCP server entry", register)
    elif spec.mcp_command:
        line = _command_line([command, *args])
        if tool == "codex":
            plan.commands.append(f"codex mcp add {SERVER_NAME} -- {line}")
        else:
            plan.commands.append(line)
            plan.notes.append(f"Register this command as an MCP server in {spec.label}'s own "
                              f"settings; it is not written for you.")
    if not spec.verified:
        plan.notes.append(f"{spec.label} support is based on secondary sources; check it "
                          f"against the vendor's documentation.")

    # keep the working files out of git
    if (root / ".git").exists():
        _edit(plan, root / ".gitignore", "ignore the memory's working files",
              lambda old: ensure_ignored(old or ""))
    return plan
