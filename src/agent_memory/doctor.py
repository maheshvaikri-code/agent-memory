"""Report whether this repository's memory is healthy and which agent tools are wired up.

Read-mostly: a store that does not exist is not created, and a plain (legacy) .memory file is
read with AgentMemory.load(), never opened as a shared store (that open would convert it in
place, one way). Never imports `mcp`: whether it is installed is asked of the import system."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .installer.jsonmerge import has_server
from .installer.markers import has_block
from .installer.plan import SERVER_NAME
from .installer.tools import TOOLS
from .memory import AgentMemory
from .service import MemoryService, ServiceError, resolve_config

ISONL = ".isonl"


def _check(name: str, ok: bool, detail: str) -> Dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


def _mcp_check() -> Dict[str, Any]:
    absent = "not installed (needed only for `amem serve`; pip install 'agent-memory[mcp]', " \
             "Python 3.10+)"
    try:
        if importlib.util.find_spec("mcp") is None:
            return _check("mcp", True, absent)
    except (ImportError, ValueError):
        return _check("mcp", True, absent)
    try:
        from importlib.metadata import version
        return _check("mcp", True, f"mcp {version('mcp')}")
    except Exception:
        return _check("mcp", True, "mcp (version unknown)")


def _tool_installed(root: Path, key: str) -> bool:
    spec = TOOLS[key]
    path = root / spec.instruction_path
    if not path.exists() or not has_block(path.read_text(encoding="utf-8", errors="replace")):
        return False
    if spec.mcp_json_path:
        json_path = root / spec.mcp_json_path
        return json_path.exists() and has_server(
            json_path.read_text(encoding="utf-8", errors="replace"), SERVER_NAME)
    return True


def _tool_check(root: Path, key: str) -> Dict[str, Any]:
    try:
        installed = _tool_installed(root, key)
    except OSError as exc:
        return _check(f"tool:{key}", False, f"its config cannot be read ({exc.strerror or 'error'})")
    if not installed:
        return _check(f"tool:{key}", True, "not installed")
    if TOOLS[key].mcp_command:
        # the AGENTS.md block is shared and the server is registered in the tool's own settings
        return _check(f"tool:{key}", True, "installed (instruction block; the MCP registration "
                                           "lives in the tool's own settings and is not checked)")
    return _check(f"tool:{key}", True, "installed")


def _open_checks(config) -> List[Dict[str, Any]]:
    """`opens`, `journal` and `warnings` for a store that exists."""
    store = config.store_path
    plain = not os.path.exists(os.path.realpath(store) + ".journal")
    try:
        if plain:
            try:
                memory = AgentMemory.load(store, repo_root=config.repo_root)
            except OSError as exc:
                raise ServiceError("store_unreadable", exc.strerror or "cannot be read") from None
            except Exception as exc:
                raise ServiceError("store_corrupt", f"not a readable memory file "
                                                    f"({type(exc).__name__})") from None
            service = MemoryService(config, memory=memory)
        else:
            service = MemoryService(config)
        try:
            status = service.status()
        finally:
            # not service.close(): that folds other processes' journal into the snapshot, a
            # write the doctor has no business making (this releases the store as it is)
            service.memory.close()
    except ServiceError as exc:
        return [_check("opens", False, f"{exc.code}: {exc.message}")]
    active = f"{status['total_active']} active notes"
    checks = [_check("opens", True, active if not plain else
                     f"{active} (a plain store: `amem serve`, the agent commands and the "
                     "legacy write commands convert it in place, one way, to a shared store)")]
    checks.append(_check("journal", True,
                         f"{status['unfolded_transactions']} unfolded transaction(s)"))
    warnings = status["warnings"]
    checks += [_check("warnings", False, w) for w in warnings] or [_check("warnings", True, "none")]
    return checks


def run_doctor(repo_root, store_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """The checks, in order: python, mcp, repo, store, then (for an existing store) opens,
    journal, warnings and lock, then one `tool:<key>` per installer tool. A pure function of its
    arguments: environment variables are resolved by the caller (the `doctor` command)."""
    root = Path(repo_root)
    checks = [_check("python", True, sys.version.split()[0]), _mcp_check()]
    if not root.is_dir():
        checks.append(_check("repo", False, f"{root} is not a directory"))
        return checks
    checks.append(_check("repo", True, str(root) if (root / ".git").exists() else
                         f"{root} (no .git here: this directory is used as the root)"))
    config = resolve_config(repo=str(root), store=store_path, env={})
    store = Path(config.store_path)
    if store.suffix == ISONL:
        checks.append(_check("store", False, f"{store}: a shared store cannot use the {ISONL} "
                                             "format; use an ISON path such as project.memory"))
    elif not store.exists():
        checks.append(_check("store", True, f"{store} will be created on first use"))
    else:
        checks.append(_check("store", True, str(store)))
        checks += _open_checks(config)
        present = [n for n in (".lock", ".journal") if Path(config.store_path + n).exists()]
        checks.append(_check("lock", True, ", ".join(present) or "no lock or journal files"))
    for key in sorted(TOOLS):
        checks.append(_tool_check(root, key))
    return checks
