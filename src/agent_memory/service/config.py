"""Where the store lives, who is writing, decided once at startup (never by a tool argument)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class ServiceConfig:
    store_path: str
    repo_root: str
    agent: str = "cli"
    allow_must: bool = False


def sanitize_agent(name: str) -> str:
    """A safe author name: letters, digits and _.@- only, at most 64 characters."""
    cleaned = re.sub(r"[^A-Za-z0-9_.@-]", "-", str(name or "").strip())[:64]
    return cleaned or "agent"


def find_repo_root(start: Optional[str] = None) -> str:
    """The nearest directory at or above `start` (default: the working directory) holding a
    .git entry, else `start` itself."""
    current = Path(start or os.getcwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return str(current)


def resolve_config(*, store: Optional[str] = None, repo: Optional[str] = None,
                   agent: Optional[str] = None, allow_must: bool = False,
                   env: Optional[Mapping[str, str]] = None,
                   cwd: Optional[str] = None) -> ServiceConfig:
    """Arguments win over environment variables, which win over defaults:
    store -> $AGENT_MEMORY_STORE -> <repo>/.agent-memory/project.memory;
    repo  -> $AGENT_MEMORY_REPO  -> git top-level of the working directory;
    agent -> $AGENT_MEMORY_AGENT -> "cli"."""
    env = os.environ if env is None else env
    repo_root = str(Path(repo or env.get("AGENT_MEMORY_REPO") or find_repo_root(cwd)).resolve())
    store_path = store or env.get("AGENT_MEMORY_STORE") or str(
        Path(repo_root) / ".agent-memory" / "project.memory")
    name = agent or env.get("AGENT_MEMORY_AGENT") or "cli"
    return ServiceConfig(os.path.abspath(store_path), repo_root, sanitize_agent(name), allow_must)
