"""Where the store lives, who is writing, decided once at startup (never by a tool argument)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional


@dataclass(frozen=True)
class ServiceConfig:
    store_path: str
    repo_root: str
    agent: str = "cli"
    allow_must: bool = False
    # Read the store without ever writing it: a missing store is not created, an existing one
    # is loaded (snapshot plus journal replayed) and never checkpointed, and every write
    # method refuses. For read-only commands (`amem status`, `recall`, `guard`).
    read_only: bool = False
    # Show the caller no absolute path of this machine (remote serving): `status` reports file
    # and directory names only, and the adapter scrubs error messages with path_scrubber.
    redact_paths: bool = False


def sanitize_agent(name: str) -> str:
    """A safe author name: letters, digits and _.@- only, at most 64 characters."""
    cleaned = re.sub(r"[^A-Za-z0-9_.@-]", "-", str(name or "").strip())[:64]
    return cleaned or "agent"


def path_scrubber(config: ServiceConfig) -> Callable[[str], str]:
    """A function that replaces this service's store path, store directory and repo root in a
    text (also in their forward-slash and resolved forms) with their last path component."""
    pairs = []
    for full in {config.store_path, os.path.dirname(config.store_path), config.repo_root}:
        if not full:
            continue
        for variant in {full, full.replace("\\", "/"), os.path.realpath(full)}:
            name = os.path.basename(variant.rstrip("/\\")) or "."
            pairs.append((variant, name))
    pairs.sort(key=lambda pair: -len(pair[0]))       # the longest path first

    def scrub(text: str) -> str:
        for old, new in pairs:
            if old and old in text:
                text = text.replace(old, new)
        return text

    return scrub


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
                   cwd: Optional[str] = None, read_only: bool = False) -> ServiceConfig:
    """Arguments win over environment variables, which win over defaults:
    store -> $AGENT_MEMORY_STORE -> <repo>/.agent-memory/project.memory;
    repo  -> $AGENT_MEMORY_REPO  -> git top-level of the working directory;
    agent -> $AGENT_MEMORY_AGENT -> "cli"."""
    env = os.environ if env is None else env
    repo_root = str(Path(repo or env.get("AGENT_MEMORY_REPO") or find_repo_root(cwd)).resolve())
    store_path = store or env.get("AGENT_MEMORY_STORE") or str(
        Path(repo_root) / ".agent-memory" / "project.memory")
    name = agent or env.get("AGENT_MEMORY_AGENT") or "cli"
    return ServiceConfig(os.path.abspath(store_path), repo_root, sanitize_agent(name), allow_must,
                         read_only)
