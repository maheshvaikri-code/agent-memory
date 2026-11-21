# Remote MCP Server (3b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the eleven memory tools over streamable HTTP behind named bearer tokens with a read-only or read-write role, so hosted agents (Base44, possibly Devin) can use the memory.

**Architecture:** A small ASGI guard (`remote/limits.py`) authenticates every request against a hashed-token file, caps the body, limits failed guesses and exposes the caller as a context variable. Behind it runs the existing FastMCP app (`mcp_server.build_server`, now taking a per-request service provider). One `MemoryService` per token is opened lazily over the same store (`remote/server.py`); read-only tokens get a three-method view. `amem token ...` manages the file and `amem serve --http` runs it under `uvicorn`.

**Tech Stack:** Python 3.10+ for the remote parts only (core stays 3.9+), `mcp>=1.25,<2` (brings `uvicorn` and `starlette`), `click`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-11-20-remote-mcp-design.md` (the binding authority; Task 7 records the corrections below).

**Branch:** work on `feature/remote-mcp` (it exists and holds the spec commit). Never push; never touch remotes or git config. The git author is already configured. **No `Co-Authored-By` or any other attribution line on any commit.**

## Global Constraints

- Core package stays Python 3.9+. `remote/tokens.py` must import without `mcp`; `remote/limits.py` must import without `mcp`; only `remote/server.py` and `mcp_server.py` import `mcp`, and `cli_remote.py` / `cli_agent.py` import them lazily inside functions.
- `mcp_server.py` must not use `from __future__ import annotations`, and every free-text tool parameter stays plain `str=""` (FastMCP JSON-parses any other annotation).
- Tool descriptions, the protocol block (`protocol.block("mcp")`) and the installer goldens do not change. The eleven tool names and their order do not change.
- Tokens: `amt_` + 32 random bytes base64url without padding; only the SHA-256 hex hash is stored; names match `[A-Za-z0-9_.@-]{1,64}` and are unique; roles are exactly `read-only` and `read-write`.
- A token never appears in a log line, an exception message, a response body, `repr` or a file other than the one-time `create` output.
- `allow_must` is always False for remote callers. The repo root and store path come only from startup arguments. No MCP tool can review, accept, reject or promote a lesson.
- Limits: request bodies over 1,000,000 bytes get 413; 20 failed authentications within 60 seconds make the next invalid token get 429, and a valid token always passes.
- Responses: 401 with `WWW-Authenticate: Bearer` and the fixed body `{"error": "unauthorized"}` for a missing, malformed, duplicate, wrong, expired or revoked credential; 429 `{"error": "too_many_failures"}` with `Retry-After: 60`; 413 `{"error": "payload_too_large"}`. Host not allowed is 421 and Origin not allowed is 403 (FastMCP's own checks, observed in the spike; the spec's "421 for both" is corrected in Task 7).
- Error code `forbidden` is added to `CODES`. A read-only token calling a write tool gets it before the service is reached.
- No absolute path of the server machine reaches a caller: `status` reports file and directory names; tool error messages pass through `path_scrubber`.
- Run tests as `python -m pytest tests -q -p no:cacheprovider`; eval as `PYTHONPATH=src python -m tests.eval.run --service`. Source files in this repo are mostly CRLF (`record.py` is LF): preserve each file's line endings (edit with a tool that does, never rewrite a whole file). The laptop CPU throttles, so `test_recall_latency_guard` can fail alone in a full run; re-run it alone before treating it as a regression.

## Review Focus

Failure modes the spec implies but no task's test obviously covers; each has a test in the named task.

1. **Header oddities** (Task 4): a lowercase `bearer` scheme must work (RFC 7235: case-insensitive); two `Authorization` headers, extra spaces, or an empty token must be 401.
2. **Token file edited while serving** (Task 2): an empty, half-written or deleted file fails closed and recovers by itself once the file is valid again.
3. **First use by two requests at once** (Task 5): exactly one `MemoryService` per principal, even when two requests with the same new token race.
4. **A hosted agent retrying a write** (Task 5): the same `memory_remember` sent twice stores one note; a repeated capture skips the duplicate.
5. **Server-absolute or Windows-style paths in `files`** (Task 5): a path outside the repo is refused with `path_outside_repo` and the response does not contain the server's repo root or temp directory.

---

## File structure

| File | Responsibility |
|---|---|
| `src/agent_memory/service/errors.py` | add code `forbidden` |
| `src/agent_memory/service/config.py` | `ServiceConfig.redact_paths`, `path_scrubber` |
| `src/agent_memory/service/service.py` | `status()` honours `redact_paths` |
| `src/agent_memory/remote/__init__.py` | package marker |
| `src/agent_memory/remote/tokens.py` | token file: create, list, revoke, verify, reload |
| `src/agent_memory/remote/limits.py` | `GuardMiddleware`, `FailureLimiter`, `current_principal`, audit logger |
| `src/agent_memory/remote/server.py` | `ReadOnlyView`, `Principals`, `build_app`, `check_startup`, `run_http` |
| `src/agent_memory/mcp_server.py` | provider seam, `TOOL_METHODS`, `READ_ONLY_TOOLS`, role gate, observer, scrub |
| `src/agent_memory/cli_remote.py` | `amem token create|list|revoke`, `serve_http` helper |
| `src/agent_memory/cli_agent.py`, `cli.py` | `serve --http` options; register `token` |
| `tests/test_service_redaction.py`, `test_remote_tokens.py`, `test_mcp_roles.py`, `test_remote_guard.py`, `test_remote_app.py`, `test_cli_remote.py`, `test_remote_e2e.py` | tests |
| `README.md`, spec, 3a spec status line | docs |

---

### Task 1: `forbidden` code and path redaction in the service

**Files:**
- Modify: `src/agent_memory/service/errors.py`
- Modify: `src/agent_memory/service/config.py`
- Modify: `src/agent_memory/service/service.py` (`_status`)
- Modify: `src/agent_memory/service/__init__.py` (export `path_scrubber`)
- Test: `tests/test_service_redaction.py`

**Interfaces:**
- Consumes: `ServiceConfig(store_path, repo_root, agent, allow_must, read_only)`, `MemoryService.status()`.
- Produces: `CODES` contains `"forbidden"`; `ServiceConfig.redact_paths: bool = False`; `path_scrubber(config) -> Callable[[str], str]` replacing the store path, the store directory and the repo root (also their forward-slash and `realpath` forms) with their final path component; `status()` returns `store` and `repo_root` as final components and scrubbed `warnings` when `redact_paths` is true.

- [ ] **Step 1: Write the failing tests**

```python
import json
import os

import pytest

from agent_memory.service import CODES, MemoryService, ServiceConfig, path_scrubber, resolve_config


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "secretrepo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


def test_forbidden_is_a_service_error_code():
    assert "forbidden" in CODES


def test_status_shows_full_paths_by_default(repo):
    svc = MemoryService(resolve_config(repo=str(repo), agent="a", env={}))
    try:
        status = svc.status()
    finally:
        svc.close()
    assert status["repo_root"] == str(repo) and status["store"].endswith("project.memory")
    assert os.path.isabs(status["store"])


def test_status_with_redact_paths_names_files_and_directories_only(repo, tmp_path):
    config = resolve_config(repo=str(repo), agent="a", env={})
    svc = MemoryService(ServiceConfig(config.store_path, config.repo_root, "a", False, False,
                                      redact_paths=True))
    try:
        svc.remember("quirk", {"description": "a thing"}, files=["src/cache.py"])
        status = svc.status()
    finally:
        svc.close()
    assert status["store"] == "project.memory" and status["repo_root"] == "secretrepo"
    assert all(str(tmp_path) not in str(warning) for warning in status["warnings"])
    assert status["total_active"] == 1


def test_path_scrubber_replaces_every_form_of_the_server_paths(repo):
    config = resolve_config(repo=str(repo), agent="a", env={})
    scrub = path_scrubber(config)
    store, root = config.store_path, config.repo_root
    text = (f"cannot read {store}; the directory {os.path.dirname(store)} and "
            f"{root.replace(os.sep, '/')}/src/x.py are missing")
    out = scrub(text)
    assert root not in out and store not in out and root.replace(os.sep, "/") not in out
    assert "project.memory" in out and "secretrepo" in out


def test_path_scrubber_leaves_other_text_alone(repo):
    scrub = path_scrubber(resolve_config(repo=str(repo), agent="a", env={}))
    assert scrub("a note about src/cache.py") == "a note about src/cache.py"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_service_redaction.py -q -p no:cacheprovider`
Expected: FAIL (`path_scrubber` cannot be imported).

- [ ] **Step 3: Implement**

`errors.py`: add `"forbidden"` to `CODES` (after `"queue_full"`, before `"internal"`).

`config.py`: add the field to `ServiceConfig` after `read_only` and add the scrubber below `sanitize_agent`:

```python
    # Show the caller no absolute path of this machine (remote serving): `status` reports file
    # and directory names only, and the adapter scrubs error messages with path_scrubber.
    redact_paths: bool = False
```

```python
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
```
(import `Callable` from `typing` in `config.py`.)

`service.py` `_status`: replace the final return so it reads the config flag:

```python
        store, root, warnings = self.config.store_path, self.config.repo_root, self.memory.warnings
        if self.config.redact_paths:
            scrub = path_scrubber(self.config)
            store, root = scrub(store), scrub(root)
            warnings = [scrub(str(w)) for w in warnings]
        return {"store": store, "store_exists": self._store_exists,
                "repo_root": root,
                ... the rest of the dict unchanged, with "warnings": warnings ...}
```
Import `path_scrubber` from `.config` in `service.py` and export it from `service/__init__.py` (`__all__` too).

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_service_redaction.py tests/test_service_core.py -q -p no:cacheprovider`
Expected: all pass (no existing status test changes: `redact_paths` defaults to False).

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/service tests/test_service_redaction.py
git commit -m "feat: forbidden error code and path redaction for remote callers"
```

---

### Task 2: The token file

**Files:**
- Create: `src/agent_memory/remote/__init__.py` (docstring only)
- Create: `src/agent_memory/remote/tokens.py`
- Test: `tests/test_remote_tokens.py`

**Interfaces:**
- Consumes: `agent_memory.storage.atomic.atomic_write_text(path, text)`.
- Produces:
  - `ROLES = ("read-only", "read-write")`, `TOKEN_PREFIX = "amt_"`, `class TokenError(Exception)`.
  - `@dataclass(frozen=True) TokenEntry(name, role, hash, created_at, expires_at)` (`expires_at == ""` means never; times are `YYYY-MM-DDTHH:MM:SSZ`).
  - `@dataclass(frozen=True) Principal(name, role, created_at)`.
  - `hash_token(token) -> str`, `new_token() -> str`.
  - `class TokenFile(path)`: `create(name, role, expires_in_days=None, now=None) -> str` (the token, once), `revoke(name) -> bool`, `entries() -> List[TokenEntry]` (as read now), `verify(token, now=None) -> Optional[Principal]`, `usable(now=None) -> int` (unexpired entries), property `error: str` (non-empty when the file is unreadable), property `generation: int` (changes whenever the file is re-read), property `path`.

- [ ] **Step 1: Write the failing tests**

```python
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from agent_memory.remote.tokens import (ROLES, TOKEN_PREFIX, Principal, TokenError, TokenFile,
                                        hash_token, new_token)


@pytest.fixture
def tf(tmp_path):
    return TokenFile(tmp_path / "tokens.json")


def test_a_new_token_has_the_prefix_and_enough_entropy():
    token = new_token()
    assert token.startswith(TOKEN_PREFIX) and len(token) == len(TOKEN_PREFIX) + 43
    assert new_token() != token


def test_create_returns_the_token_once_and_the_file_holds_only_its_hash(tf):
    token = tf.create("base44-prod", "read-write")
    raw = tf.path.read_text(encoding="utf-8")
    assert token not in raw and hash_token(token) in raw
    entry = json.loads(raw)["tokens"][0]
    assert entry["name"] == "base44-prod" and entry["role"] == "read-write"
    assert set(entry) == {"name", "role", "hash", "created_at", "expires_at"}


def test_verify_returns_the_principal_for_a_good_token_and_none_otherwise(tf):
    token = tf.create("alice", "read-only")
    principal = tf.verify(token)
    assert principal == Principal("alice", "read-only", principal.created_at)
    assert tf.verify(token + "x") is None and tf.verify("") is None and tf.verify("amt_nope") is None


def test_names_are_validated_and_unique(tf):
    for bad in ["", " ", "a b", "x" * 65, "naïve", "a/b"]:
        with pytest.raises(TokenError):
            tf.create(bad, "read-only")
    tf.create("alice", "read-only")
    with pytest.raises(TokenError, match="already"):
        tf.create("alice", "read-write")


def test_roles_are_a_closed_set(tf):
    assert ROLES == ("read-only", "read-write")
    with pytest.raises(TokenError):
        tf.create("alice", "admin")


def test_expiry(tf):
    token = tf.create("alice", "read-write", expires_in_days=1)
    now = datetime.now(timezone.utc)
    assert tf.verify(token, now=now) is not None
    assert tf.verify(token, now=now + timedelta(days=2)) is None
    assert tf.usable(now=now + timedelta(days=2)) == 0 and tf.usable(now=now) == 1
    with pytest.raises(TokenError):
        tf.create("bob", "read-only", expires_in_days=0)


def test_revoke_applies_on_the_next_verify_without_a_restart(tf):
    token = tf.create("alice", "read-write")
    assert tf.verify(token) is not None
    assert tf.revoke("alice") is True and tf.revoke("alice") is False
    assert tf.verify(token) is None


def test_another_process_editing_the_file_is_noticed(tf, tmp_path):
    token = tf.create("alice", "read-write")
    other = TokenFile(tmp_path / "tokens.json")           # as the `amem token` command would
    other.revoke("alice")
    assert tf.verify(token) is None


@pytest.mark.parametrize("content", ["", "{", "[]", '{"version": 2, "tokens": []}',
                                     '{"version": 1, "tokens": [{"name": "a"}]}',
                                     '{"version": 1, "tokens": [{"name": "a", "role": "root", '
                                     '"hash": "x", "created_at": "", "expires_at": ""}]}'])
def test_a_damaged_file_fails_closed_and_recovers(tf, content):
    token = tf.create("alice", "read-write")
    good = tf.path.read_text(encoding="utf-8")
    tf.path.write_text(content, encoding="utf-8")
    assert tf.verify(token) is None and tf.error and tf.usable() == 0
    tf.path.write_text(good, encoding="utf-8")
    os.utime(tf.path, (1, 2 ** 31 - 1))                    # a new modification time
    assert tf.verify(token) is not None and tf.error == ""


def test_a_missing_file_means_no_tokens_not_an_error(tmp_path):
    tf = TokenFile(tmp_path / "none.json")
    assert tf.verify("amt_x") is None and tf.usable() == 0 and tf.error == ""


def test_generation_changes_when_the_file_is_reread(tf):
    tf.create("alice", "read-only")
    first = tf.generation
    tf.create("bob", "read-only")
    tf.entries()
    assert tf.generation != first


@pytest.mark.skipif(os.name == "nt", reason="file modes are best effort on Windows")
def test_the_file_is_private(tf):
    tf.create("alice", "read-only")
    assert stat.S_IMODE(os.stat(tf.path).st_mode) == 0o600


def test_a_token_is_not_in_the_repr_or_an_error(tf):
    token = tf.create("alice", "read-only")
    assert token not in repr(tf) and token not in repr(tf.verify(token))
    with pytest.raises(TokenError) as excinfo:
        tf.create("alice", "read-only")
    assert token not in str(excinfo.value)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_remote_tokens.py -q -p no:cacheprovider`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `remote/__init__.py` and `remote/tokens.py`**

`remote/__init__.py`: `"""Remote (HTTP) serving of the memory: tokens, the guard and the server."""`

```python
"""Bearer tokens for the remote server: a JSON file of hashes, never the tokens themselves."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from ..storage.atomic import atomic_write_text

ROLES = ("read-only", "read-write")
TOKEN_PREFIX = "amt_"
FILE_VERSION = 1
MAX_EXPIRY_DAYS = 3650
_NAME = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
_TIME = "%Y-%m-%dT%H:%M:%SZ"


class TokenError(Exception):
    """A token file or token request that cannot be honoured. Never carries a token."""


@dataclass(frozen=True)
class TokenEntry:
    name: str
    role: str
    hash: str
    created_at: str
    expires_at: str = ""


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    created_at: str


def new_token() -> str:
    return TOKEN_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIME)


def _parse(stamp: str) -> Optional[datetime]:
    try:
        return datetime.strptime(stamp, _TIME).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class TokenFile:
    def __init__(self, path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._signature = None
        self._entries: List[TokenEntry] = []
        self._error = ""
        self._generation = 0

    def __repr__(self) -> str:
        return f"TokenFile({str(self._path)!r})"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def error(self) -> str:
        with self._lock:
            self._refresh()
            return self._error

    @property
    def generation(self) -> int:
        with self._lock:
            self._refresh()
            return self._generation

    # -- reading ---------------------------------------------------------------

    def _refresh(self) -> None:
        """Re-read the file when it changed. Any problem leaves no usable token (fail closed)."""
        try:
            stat_result = os.stat(self._path)
            signature = (stat_result.st_mtime_ns, stat_result.st_size)
        except FileNotFoundError:
            signature = None
        if signature == self._signature and self._generation:
            return
        self._signature = signature
        self._generation += 1
        if signature is None:
            self._entries, self._error = [], ""
            return
        try:
            self._entries, self._error = self._parse_file(), ""
        except TokenError as exc:
            self._entries, self._error = [], str(exc)

    def _parse_file(self) -> List[TokenEntry]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TokenError(f"the token file cannot be read: {type(exc).__name__}") from None
        if not isinstance(data, dict) or data.get("version") != FILE_VERSION \
                or not isinstance(data.get("tokens"), list):
            raise TokenError("the token file is not a version 1 token file")
        entries, names = [], set()
        for raw in data["tokens"]:
            try:
                entry = TokenEntry(raw["name"], raw["role"], raw["hash"], raw["created_at"],
                                   raw.get("expires_at", ""))
            except (KeyError, TypeError):
                raise TokenError("a token entry is missing a field") from None
            if not _NAME.fullmatch(str(entry.name)) or entry.role not in ROLES \
                    or not re.fullmatch(r"[0-9a-f]{64}", str(entry.hash)) \
                    or entry.name in names:
                raise TokenError("a token entry has an invalid name, role or hash")
            names.add(entry.name)
            entries.append(entry)
        return entries

    def entries(self) -> List[TokenEntry]:
        with self._lock:
            self._refresh()
            return list(self._entries)

    def usable(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        return sum(1 for entry in self.entries() if not self._expired(entry, now))

    @staticmethod
    def _expired(entry: TokenEntry, now: datetime) -> bool:
        if not entry.expires_at:
            return False
        moment = _parse(entry.expires_at)
        return moment is None or moment <= now         # an unreadable expiry counts as expired

    def verify(self, token: str, now: Optional[datetime] = None) -> Optional[Principal]:
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
            return None
        now = now or datetime.now(timezone.utc)
        digest = hash_token(token)
        found = None
        for entry in self.entries():                   # every entry is compared: no early exit
            if hmac.compare_digest(entry.hash, digest):
                found = entry
        if found is None or self._expired(found, now):
            return None
        return Principal(found.name, found.role, found.created_at)

    # -- writing ---------------------------------------------------------------

    def _write(self, entries: List[TokenEntry]) -> None:
        body = {"version": FILE_VERSION, "tokens": [
            {"name": e.name, "role": e.role, "hash": e.hash, "created_at": e.created_at,
             "expires_at": e.expires_at} for e in entries]}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            descriptor = os.open(self._path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        atomic_write_text(self._path, json.dumps(body, indent=2) + "\n")
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass                                       # best effort (Windows)
        self._signature = None                         # force a re-read on the next use

    def create(self, name: str, role: str, expires_in_days: Optional[int] = None,
               now: Optional[datetime] = None) -> str:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise TokenError("a name is 1-64 letters, digits or _.@-")
        if role not in ROLES:
            raise TokenError(f"a role is one of: {', '.join(ROLES)}")
        if expires_in_days is not None and not (
                isinstance(expires_in_days, int) and 1 <= expires_in_days <= MAX_EXPIRY_DAYS):
            raise TokenError(f"expires-in is a whole number of days from 1 to {MAX_EXPIRY_DAYS}")
        now = now or datetime.now(timezone.utc)
        with self._lock:
            self._signature = None
            self._refresh()
            if self._error:
                raise TokenError(f"{self._error}; fix or remove it first")
            if any(entry.name == name for entry in self._entries):
                raise TokenError(f"a token named {name!r} already exists")
            token = new_token()
            expires = _stamp(now + timedelta(days=expires_in_days)) if expires_in_days else ""
            self._write(self._entries + [TokenEntry(name, role, hash_token(token), _stamp(now),
                                                    expires)])
        return token

    def revoke(self, name: str) -> bool:
        with self._lock:
            self._signature = None
            self._refresh()
            if self._error:
                raise TokenError(f"{self._error}; fix or remove it first")
            kept = [entry for entry in self._entries if entry.name != name]
            if len(kept) == len(self._entries):
                return False
            self._write(kept)
            return True
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_remote_tokens.py -q -p no:cacheprovider`
Expected: all pass. If `test_a_damaged_file_fails_closed_and_recovers` flakes on timestamp resolution, the `os.utime` call in the test already forces a new mtime; keep it.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/remote tests/test_remote_tokens.py
git commit -m "feat: hashed bearer-token file with roles, expiry and fail-closed reload"
```

---

### Task 3: The provider seam, roles and scrubbing in `mcp_server`

**Files:**
- Modify: `src/agent_memory/mcp_server.py`
- Test: `tests/test_mcp_roles.py`

**Interfaces:**
- Consumes: `MemoryService` methods `recall, check, remember, feedback, supersede, retract, verify, session_start, session_end, capture, status`; `ServiceError("forbidden", ...)` from Task 1.
- Produces: `TOOL_METHODS: Dict[str, str]` (tool name to service method; its key order equals the existing `TOOL_NAMES`), `TOOL_NAMES = tuple(TOOL_METHODS)`, `READ_ONLY_TOOLS = frozenset({"memory_recall", "memory_check", "memory_status"})`, and `build_server(service=None, *, provider=None, trust_client_name=True, observer=None, scrub=None, settings=None) -> FastMCP`. `provider() -> (service_like, role)` is called per tool call; `role` is `"read-write"` or `"read-only"`. `observer(tool, outcome, ms)` is called after every call (`outcome` is `"ok"` or the error code). `scrub(message) -> str` is applied to every error message that leaves. `settings` is passed to `FastMCP(...)`. `build_server(service)` keeps its old behaviour exactly (read-write, trusts the client name) and `serve(config)` is unchanged.

- [ ] **Step 1: Write the failing tests**

```python
import asyncio
import json

import pytest

mcp = pytest.importorskip("mcp")

from mcp import types                                                # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from agent_memory.mcp_server import (READ_ONLY_TOOLS, TOOL_METHODS, TOOL_NAMES,   # noqa: E402
                                     build_server)
from agent_memory.service import MemoryService, ServiceError, resolve_config  # noqa: E402

WRITE_CALLS = {
    "memory_remember": {"kind": "quirk", "fields": {"description": "d"}},
    "memory_feedback": {"id": "x", "helpful": True},
    "memory_supersede": {"old_id": "x", "kind": "quirk", "fields": {"description": "d"}},
    "memory_retract": {"id": "x"},
    "memory_verify": {"id": "x"},
    "memory_session_start": {"task": "t"},
    "memory_session_end": {},
    "memory_capture_lessons": {"lessons": [{"kind": "quirk", "fields": {"description": "d"},
                                            "evidence": "e", "files": ["src/cache.py"]}]},
}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 90))


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    return result.isError, result.content[0].text


def test_every_tool_is_classified_and_backed_by_a_service_method():
    assert tuple(TOOL_METHODS) == TOOL_NAMES and len(TOOL_NAMES) == 11
    assert READ_ONLY_TOOLS == {"memory_recall", "memory_check", "memory_status"}
    assert set(WRITE_CALLS) == set(TOOL_NAMES) - READ_ONLY_TOOLS
    for method in TOOL_METHODS.values():
        assert callable(getattr(MemoryService, method))


def test_a_read_only_caller_is_refused_every_write_tool_and_nothing_is_written(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="ro", env={}))

    async def scenario():
        server = build_server(provider=lambda: (service, "read-only"))
        async with create_connected_server_and_client_session(server) as client:
            refused = {name: await call(client, name, **args) for name, args in WRITE_CALLS.items()}
            allowed = [await call(client, "memory_recall", task="anything"),
                       await call(client, "memory_check", approach="anything"),
                       await call(client, "memory_status")]
            return refused, allowed

    refused, allowed = run(scenario())
    try:
        assert service.status()["total_active"] == 0 and service.status()["pending"] == 0
    finally:
        service.close()
    assert all(is_error and "forbidden" in text for is_error, text in refused.values())
    assert all(not is_error for is_error, _ in allowed)


def test_a_read_write_provider_can_use_every_tool(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="rw", env={}))

    async def scenario():
        server = build_server(provider=lambda: (service, "read-write"))
        async with create_connected_server_and_client_session(server) as client:
            return await call(client, "memory_remember", kind="quirk",
                              fields={"description": "a thing"})

    is_error, text = run(scenario())
    service.close()
    assert not is_error and json.loads(text)["created"] is True


def test_the_client_reported_name_is_ignored_when_not_trusted(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="alice", env={}))

    async def scenario():
        server = build_server(provider=lambda: (service, "read-write"), trust_client_name=False)
        async with create_connected_server_and_client_session(
                server, client_info=types.Implementation(name="evil-bot", version="1")) as client:
            return await call(client, "memory_remember", kind="quirk",
                              fields={"description": "who wrote me"})

    _, text = run(scenario())
    node = service.memory.graph.get_node("quirk", json.loads(text)["id"])
    service.close()
    assert node.properties["agent"] == "alice"


def test_the_client_reported_name_is_still_used_for_stdio(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="cli", env={}))

    async def scenario():
        async with create_connected_server_and_client_session(
                build_server(service),
                client_info=types.Implementation(name="claude-code", version="1")) as client:
            return await call(client, "memory_remember", kind="quirk",
                              fields={"description": "who wrote me"})

    _, text = run(scenario())
    node = service.memory.graph.get_node("quirk", json.loads(text)["id"])
    service.close()
    assert node.properties["agent"] == "claude-code"


def test_the_observer_sees_every_call_with_its_outcome(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="ro", env={}))
    seen = []

    async def scenario():
        server = build_server(provider=lambda: (service, "read-only"),
                              observer=lambda tool, outcome, ms: seen.append((tool, outcome, ms)))
        async with create_connected_server_and_client_session(server) as client:
            await call(client, "memory_recall", task="t")
            await call(client, "memory_retract", id="x")

    run(scenario())
    service.close()
    assert [(t, o) for t, o, _ in seen] == [("memory_recall", "ok"), ("memory_retract", "forbidden")]
    assert all(isinstance(ms, int) and ms >= 0 for _, _, ms in seen)


def test_error_messages_pass_through_the_scrubber(repo):
    class Broken:
        def set_client(self, name):
            pass

        def status(self):
            raise ServiceError("store_corrupt", "cannot read C:/secret/dir/project.memory")

    async def scenario():
        server = build_server(provider=lambda: (Broken(), "read-write"),
                              scrub=lambda text: text.replace("C:/secret/dir", "<dir>"))
        async with create_connected_server_and_client_session(server) as client:
            return await call(client, "memory_status")

    is_error, text = run(scenario())
    assert is_error and "C:/secret" not in text and "<dir>/project.memory" in text


def test_a_provider_that_fails_closed_gives_an_error_not_a_crash(repo):
    def provider():
        raise ServiceError("forbidden", "no authenticated caller")

    async def scenario():
        async with create_connected_server_and_client_session(build_server(provider=provider)) as c:
            return await call(c, "memory_status")

    is_error, text = run(scenario())
    assert is_error and "forbidden" in text


def test_build_server_needs_exactly_one_of_service_and_provider(repo):
    with pytest.raises(ValueError):
        build_server()
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    try:
        with pytest.raises(ValueError):
            build_server(service, provider=lambda: (service, "read-write"))
    finally:
        service.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_mcp_roles.py -q -p no:cacheprovider`
Expected: FAIL (`READ_ONLY_TOOLS` cannot be imported).

- [ ] **Step 3: Implement**

Edit `mcp_server.py` (keep CRLF; keep the tool descriptions byte for byte):

1. Imports: add `import time`, and `Tuple` to the typing import.
2. Replace the `TOOL_NAMES = (...)` tuple with:

```python
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
```

3. Replace `build_server`'s signature and `run` (the eleven tool bodies change only in how they call `run`):

```python
def build_server(service: Optional[MemoryService] = None, *,
                 provider: Optional[Callable[[], Tuple[Any, str]]] = None,
                 trust_client_name: bool = True,
                 observer: Optional[Callable[[str, str, int], None]] = None,
                 scrub: Optional[Callable[[str], str]] = None,
                 settings: Optional[Dict[str, Any]] = None) -> FastMCP:
    """`service` serves stdio: one service, read-write, the client's own name stamped on writes.
    A `provider` serves many callers: it returns (service, role) for the current call, and the
    client-reported name is not trusted unless asked. `observer` hears every call's outcome and
    `scrub` cleans every error message that leaves."""
    if (service is None) == (provider is None):
        raise ValueError("pass either a service or a provider")
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
            if trust_client_name:
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
```

4. In each tool body, replace the first arguments of `run(...)`: `run(ctx, service.recall, task=...)` becomes `run(ctx, "memory_recall", task=...)` and likewise `memory_check`, `memory_remember`, `memory_feedback`, `memory_supersede`, `memory_retract`, `memory_verify`, `memory_session_start`, `memory_session_end`, `memory_capture_lessons`, `memory_status`. The keyword arguments passed stay exactly as they are.

5. `serve(config)` is unchanged.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_mcp_roles.py tests/test_mcp_server.py tests/test_mcp_capture.py tests/test_mcp_concurrency.py tests/test_installer_tools.py -q -p no:cacheprovider`
Expected: all pass (existing MCP tests and installer goldens untouched).

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/mcp_server.py tests/test_mcp_roles.py
git commit -m "feat: per-call service provider, read-only role gate and error scrubbing in the MCP server"
```

---

### Task 4: The guard: authentication, body cap and failure limiter

**Files:**
- Create: `src/agent_memory/remote/limits.py`
- Test: `tests/test_remote_guard.py`

**Interfaces:**
- Consumes: `TokenFile.verify`, `Principal`.
- Produces: `MAX_BODY_BYTES = 1_000_000`, `MAX_FAILURES = 20`, `FAILURE_WINDOW = 60.0`, `audit_log` (a `logging.Logger` named `agent_memory.remote.audit`), `current_principal` (`ContextVar[Optional[Principal]]`), `FailureLimiter(max_failures, window, clock)` with `blocked()` and `record()`, and `GuardMiddleware(app, tokens, limiter=None, max_body=MAX_BODY_BYTES)`, an ASGI app wrapper. It answers 401/429/413 itself, buffers a request body up to the cap, sets `current_principal` for the wrapped app, and passes non-HTTP scopes (lifespan) straight through. It must not import `mcp`.

- [ ] **Step 1: Write the failing tests**

```python
import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_memory.remote.limits import (FailureLimiter, GuardMiddleware, current_principal)
from agent_memory.remote.tokens import TokenFile

UNAUTHORIZED = {"error": "unauthorized"}


async def echo(request):
    body = await request.body()
    principal = current_principal.get()
    return JSONResponse({"who": principal.name if principal else None,
                         "role": principal.role if principal else None, "bytes": len(body)})


def inner():
    return Starlette(routes=[Route("/mcp", echo, methods=["GET", "POST", "DELETE"])])


@pytest.fixture
def tokens(tmp_path):
    return TokenFile(tmp_path / "tokens.json")


@pytest.fixture
def client(tokens):
    return TestClient(GuardMiddleware(inner(), tokens, max_body=100))


def bearer(token, scheme="Bearer"):
    return {"Authorization": f"{scheme} {token}"}


def assert_denied(response):
    assert response.status_code == 401 and response.json() == UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"


def test_no_credential_is_401_with_a_fixed_body(client, tokens):
    tokens.create("alice", "read-write")
    assert_denied(client.post("/mcp", json={}))


def test_a_wrong_token_looks_exactly_like_no_token(client, tokens):
    tokens.create("alice", "read-write")
    missing = client.post("/mcp", json={})
    wrong = client.post("/mcp", json={}, headers=bearer("amt_" + "A" * 43))
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json() and wrong.headers["www-authenticate"] == "Bearer"


def test_a_good_token_reaches_the_app_with_its_identity(client, tokens):
    token = tokens.create("alice", "read-only")
    response = client.post("/mcp", json={"a": 1}, headers=bearer(token))
    assert response.status_code == 200 and response.json()["who"] == "alice"
    assert response.json()["role"] == "read-only"


def test_the_scheme_is_case_insensitive(client, tokens):
    token = tokens.create("alice", "read-write")
    assert client.post("/mcp", json={}, headers=bearer(token, "bearer")).status_code == 200
    assert client.post("/mcp", json={}, headers=bearer(token, "BEARER")).status_code == 200


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Basic abc", "Bearer a b", "token amt_x",
                                    "Bearer  amt_x"])
def test_malformed_credentials_are_401(client, tokens, header):
    tokens.create("alice", "read-write")
    assert_denied(client.post("/mcp", json={}, headers={"Authorization": header}))


def test_two_authorization_headers_are_refused_even_when_one_is_valid(client, tokens):
    token = tokens.create("alice", "read-write")
    response = client.post("/mcp", json={}, headers=[("Authorization", f"Bearer {token}"),
                                                     ("Authorization", f"Bearer {token}")])
    assert_denied(response)


def test_a_revoked_token_is_refused_on_the_next_request(client, tokens):
    token = tokens.create("alice", "read-write")
    assert client.post("/mcp", json={}, headers=bearer(token)).status_code == 200
    tokens.revoke("alice")
    assert_denied(client.post("/mcp", json={}, headers=bearer(token)))


def test_an_expired_token_is_refused(tmp_path):
    tokens = TokenFile(tmp_path / "t.json")
    token = tokens.create("alice", "read-write", expires_in_days=1,
                          now=datetime.now(timezone.utc) - timedelta(days=3))
    client = TestClient(GuardMiddleware(inner(), tokens))
    assert_denied(client.post("/mcp", json={}, headers=bearer(token)))


def test_unknown_paths_are_not_probeable_without_a_token(client):
    assert_denied(client.get("/anything"))


def test_a_body_over_the_cap_is_413_by_content_length(client, tokens):
    token = tokens.create("alice", "read-write")
    response = client.post("/mcp", content=b"x" * 101, headers=bearer(token))
    assert response.status_code == 413 and response.json() == {"error": "payload_too_large"}


def test_a_chunked_body_over_the_cap_is_413(client, tokens):
    token = tokens.create("alice", "read-write")
    response = client.post("/mcp", content=iter([b"x" * 60, b"x" * 60]), headers=bearer(token))
    assert response.status_code == 413


def test_a_body_at_the_cap_passes_whole(client, tokens):
    token = tokens.create("alice", "read-write")
    response = client.post("/mcp", content=b"x" * 100, headers=bearer(token))
    assert response.status_code == 200 and response.json()["bytes"] == 100


def test_an_oversized_body_without_a_token_is_401_not_413(client):
    assert_denied(client.post("/mcp", content=b"x" * 500))


def test_the_limiter_blocks_after_too_many_failures_but_never_a_valid_token(tokens):
    token = tokens.create("alice", "read-write")
    client = TestClient(GuardMiddleware(inner(), tokens, FailureLimiter(3, 60.0)))
    for _ in range(3):
        assert client.post("/mcp", json={}, headers=bearer("amt_bad")).status_code == 401
    blocked = client.post("/mcp", json={}, headers=bearer("amt_bad"))
    assert blocked.status_code == 429 and blocked.json() == {"error": "too_many_failures"}
    assert blocked.headers["retry-after"] == "60"
    assert client.post("/mcp", json={}).status_code == 429
    assert client.post("/mcp", json={}, headers=bearer(token)).status_code == 200


def test_the_limiter_forgets_failures_after_its_window():
    now = [0.0]
    limiter = FailureLimiter(2, 60.0, clock=lambda: now[0])
    limiter.record()
    limiter.record()
    assert limiter.blocked()
    now[0] = 61.0
    assert not limiter.blocked()


def test_the_lifespan_passes_through(tokens):
    started = []

    @contextlib.asynccontextmanager
    async def lifespan(app):
        started.append("up")
        yield
        started.append("down")

    app = Starlette(routes=[Route("/mcp", echo)], lifespan=lifespan)
    with TestClient(GuardMiddleware(app, tokens)):
        assert started == ["up"]
    assert started == ["up", "down"]


def test_denials_are_audited_without_the_presented_value(client, tokens, caplog):
    tokens.create("alice", "read-write")
    secret = "amt_" + "S" * 43
    with caplog.at_level(logging.INFO, logger="agent_memory.remote.audit"):
        client.post("/mcp", json={}, headers=bearer(secret))
        client.post("/mcp", json={})
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "reason=invalid" in text and "reason=missing" in text and secret not in text


def test_the_principal_is_not_left_set_after_a_request(tokens):
    token = tokens.create("alice", "read-write")
    seen = []

    async def app(scope, receive, send):
        seen.append(current_principal.get().name)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    guard = GuardMiddleware(app, tokens)

    async def go():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            pass

        scope = {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
        await guard(scope, receive, send)
        return current_principal.get()

    assert asyncio.run(go()) is None and seen == ["alice"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_remote_guard.py -q -p no:cacheprovider`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `remote/limits.py`**

```python
"""The guard in front of the MCP app: who is calling, how much they may send, how many wrong
guesses are tolerated. Pure ASGI; imports nothing from `mcp`."""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from collections import deque
from typing import Callable, Deque, List, Optional, Tuple

from .tokens import Principal, TokenFile

MAX_BODY_BYTES = 1_000_000
MAX_FAILURES = 20
FAILURE_WINDOW = 60.0

# One line per event: names, tool names, outcome codes and durations; never an argument, a
# result or a token.
audit_log = logging.getLogger("agent_memory.remote.audit")

current_principal: "contextvars.ContextVar[Optional[Principal]]" = contextvars.ContextVar(
    "agent_memory_principal", default=None)


class FailureLimiter:
    """Counts failed authentications in a sliding window. Global, not per address: behind a
    proxy every request shares an address."""

    def __init__(self, max_failures: int = MAX_FAILURES, window: float = FAILURE_WINDOW,
                 clock: Callable[[], float] = time.monotonic):
        self._max, self._window, self._clock = max_failures, window, clock
        self._failures: Deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self) -> None:
        horizon = self._clock() - self._window
        while self._failures and self._failures[0] <= horizon:
            self._failures.popleft()

    def blocked(self) -> bool:
        with self._lock:
            self._trim()
            return len(self._failures) >= self._max

    def record(self) -> None:
        with self._lock:
            self._trim()
            self._failures.append(self._clock())


def _bearer(headers: List[Tuple[bytes, bytes]]) -> Tuple[Optional[str], str]:
    """(token, reason): reason is "" when a single well-formed bearer credential was sent."""
    values = [value for name, value in headers if name == b"authorization"]
    if not values:
        return None, "missing"
    if len(values) > 1:
        return None, "invalid"
    parts = values[0].decode("latin-1").split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None, "invalid"
    return parts[1], ""


async def _respond(send, status: int, body: dict, extra: Optional[List[Tuple[bytes, bytes]]] = None):
    data = json.dumps(body).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(data)).encode())] + list(extra or [])})
    await send({"type": "http.response.body", "body": data})


class GuardMiddleware:
    def __init__(self, app, tokens: TokenFile, limiter: Optional[FailureLimiter] = None,
                 max_body: int = MAX_BODY_BYTES):
        self.app, self.tokens, self.max_body = app, tokens, max_body
        self.limiter = limiter if limiter is not None else FailureLimiter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":                       # lifespan and anything else
            await self.app(scope, receive, send)
            return
        token, reason = _bearer(scope["headers"])
        principal = self.tokens.verify(token) if token else None
        if principal is None:
            reason = reason or "invalid"
            if self.limiter.blocked():
                audit_log.warning("auth denied reason=%s throttled=yes", reason)
                await _respond(send, 429, {"error": "too_many_failures"}, [(b"retry-after", b"60")])
                return
            self.limiter.record()
            audit_log.warning("auth denied reason=%s", reason)
            await _respond(send, 401, {"error": "unauthorized"}, [(b"www-authenticate", b"Bearer")])
            return
        body = await self._read_body(scope, receive, send)
        if body is None:
            return
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()                        # a later disconnect

        marker = current_principal.set(principal)
        try:
            await self.app(scope, replay, send)
        finally:
            current_principal.reset(marker)

    async def _read_body(self, scope, receive, send) -> Optional[bytes]:
        """The whole request body, or None after answering 413 or 400."""
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_body:
                    await _respond(send, 413, {"error": "payload_too_large"})
                    return None
            except ValueError:
                await _respond(send, 400, {"error": "bad_request"})
                return None
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                await _respond(send, 413, {"error": "payload_too_large"})
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_remote_guard.py -q -p no:cacheprovider`
Expected: all pass. (`test_an_oversized_body_without_a_token_is_401_not_413` pins that authentication comes first.)

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/remote/limits.py tests/test_remote_guard.py
git commit -m "feat: ASGI guard with fixed 401, body cap and a failure limiter"
```

---

### Task 5: The HTTP app: per-token services, roles, hardening

**Files:**
- Create: `src/agent_memory/remote/server.py`
- Test: `tests/test_remote_app.py`

**Interfaces:**
- Consumes: `build_server(provider=..., trust_client_name=False, observer=..., scrub=..., settings=...)` (Task 3); `GuardMiddleware`, `FailureLimiter`, `current_principal`, `audit_log` (Task 4); `TokenFile`, `Principal` (Task 2); `path_scrubber`, `ServiceConfig(redact_paths=True)` (Task 1).
- Produces: `class ReadOnlyView(service)` with `recall`, `check`, `status` only; `class Principals(base_config, tokens)` with `service_for(principal)`, `open_count` property, `close()`; `build_app(base_config, tokens, *, public_url="", limiter=None) -> (asgi_app, principals)`; `class StartupError(Exception)`; `is_loopback(host) -> bool`; `check_startup(base_config, tokens, host, behind_tls)`; `run_http(base_config, tokens, host, port, public_url="", behind_tls=False)`.

- [ ] **Step 1: Write the failing tests**

```python
import concurrent.futures
import json
import logging
import threading
from datetime import datetime, timezone

import pytest

pytest.importorskip("mcp")
from starlette.testclient import TestClient                          # noqa: E402

from agent_memory.remote.server import (Principals, ReadOnlyView, StartupError, build_app,  # noqa: E402
                                        check_startup, is_loopback)
from agent_memory.remote.limits import FailureLimiter                # noqa: E402
from agent_memory.remote.tokens import Principal, TokenFile          # noqa: E402
from agent_memory.service import resolve_config                      # noqa: E402

ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "work" / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def base(repo):
    return resolve_config(repo=str(repo), agent="remote", env={})


@pytest.fixture
def tokens(tmp_path):
    return TokenFile(tmp_path / "tokens.json")


@pytest.fixture
def app_client(base, tokens):
    app, principals = build_app(base, tokens, public_url="https://mem.example.com")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield client, principals
    principals.close()


def rpc(client, token, method, params=None, headers=None, call_id=1):
    sent = dict(ACCEPT)
    if token:
        sent["Authorization"] = f"Bearer {token}"
    sent.update(headers or {})
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": call_id, "method": method,
                                     "params": params or {}}, headers=sent)


def tool(client, token, name, **arguments):
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    text = result["content"][0]["text"]
    return result["isError"], (text if result["isError"] else json.loads(text))


INIT = {"protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "evil-bot", "version": "1"}}


def test_an_unauthenticated_initialize_is_401(app_client, tokens):
    client, _ = app_client
    tokens.create("alice", "read-write")
    assert rpc(client, None, "initialize", INIT).status_code == 401


def test_the_tool_list_is_the_stdio_surface(app_client, tokens):
    from agent_memory.mcp_server import TOOL_NAMES, build_server
    from agent_memory.service import MemoryService
    client, _ = app_client
    token = tokens.create("alice", "read-write")
    remote = rpc(client, token, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in remote] == list(TOOL_NAMES)
    assert not any(word in t["name"] for t in remote
                   for word in ("review", "accept", "reject", "promote"))
    local = build_server(MemoryService.__new__(MemoryService))          # descriptions only
    import asyncio
    stdio = asyncio.run(local.list_tools())
    assert [(t["name"], t["description"]) for t in remote] == [(t.name, t.description) for t in stdio]


def test_a_read_write_token_writes_and_a_read_only_token_reads_it(app_client, tokens):
    client, _ = app_client
    rw, ro = tokens.create("alice", "read-write"), tokens.create("bob", "read-only")
    is_error, made = tool(client, rw, "memory_remember", kind="quirk",
                          fields={"description": "the zorblax queue drops items over 64KB"},
                          files=["src/cache.py"])
    assert not is_error and made["created"] is True
    is_error, found = tool(client, ro, "memory_recall", task="zorblax queue drops items")
    assert not is_error and [i["id"] for i in found["items"]] == [made["id"]]


def test_a_read_only_token_cannot_write_and_nothing_changes(app_client, tokens):
    client, _ = app_client
    ro = tokens.create("bob", "read-only")
    is_error, text = tool(client, ro, "memory_remember", kind="quirk",
                          fields={"description": "should not be stored"})
    assert is_error and "forbidden" in text
    is_error, status = tool(client, ro, "memory_status")
    assert status["total_active"] == 0


def test_notes_carry_the_token_name_whatever_the_client_claims(app_client, tokens, base):
    client, principals = app_client
    token = tokens.create("base44-prod", "read-write")
    rpc(client, token, "initialize", INIT)                       # the client says it is "evil-bot"
    _, made = tool(client, token, "memory_remember", kind="quirk",
                   fields={"description": "who wrote this"})
    service = principals.service_for(Principal("base44-prod", "read-write",
                                               tokens.entries()[0].created_at))
    assert service.memory.graph.get_node("quirk", made["id"]).properties["agent"] == "base44-prod"


def test_two_tokens_keep_separate_sessions(app_client, tokens):
    client, _ = app_client
    a, b = tokens.create("alice", "read-write"), tokens.create("bob", "read-write")
    _, first = tool(client, a, "memory_session_start", task="task a")
    _, second = tool(client, b, "memory_session_start", task="task b")
    assert first["session_id"] != second["session_id"]
    _, again = tool(client, a, "memory_session_start", task="task a again")
    assert again["already_active"] is True and again["session_id"] == first["session_id"]


def test_a_lesson_captured_remotely_is_pending_and_not_recalled(app_client, tokens):
    client, _ = app_client
    rw = tokens.create("alice", "read-write")
    is_error, out = tool(client, rw, "memory_capture_lessons", lessons=[
        {"kind": "quirk", "fields": {"description": "the retry queue drops jobs on restart"},
         "evidence": "seen in worker.py", "files": ["src/cache.py"]}])
    assert not is_error and len(out["captured"]) == 1 and out["pending"] == 1
    _, found = tool(client, rw, "memory_recall", task="retry queue drops jobs on restart")
    assert found["items"] == []


def test_a_retried_write_stores_one_note(app_client, tokens):
    client, _ = app_client
    rw = tokens.create("alice", "read-write")
    args = dict(kind="quirk", fields={"description": "idempotent under retry"})
    _, first = tool(client, rw, "memory_remember", **args)
    _, second = tool(client, rw, "memory_remember", **args)
    assert first["id"] == second["id"] and second["merged"] is True
    _, status = tool(client, rw, "memory_status")
    assert status["total_active"] == 1


def test_priority_must_is_never_available_remotely(app_client, tokens):
    client, _ = app_client
    rw = tokens.create("alice", "read-write")
    is_error, text = tool(client, rw, "memory_remember", kind="quirk",
                          fields={"description": "pin me"}, priority="must")
    assert is_error and "allow_must" in text


def test_no_absolute_server_path_reaches_the_caller(app_client, tokens, tmp_path):
    client, _ = app_client
    rw = tokens.create("alice", "read-write")
    inside = str(tmp_path / "work" / "repo" / "src" / "cache.py")     # the server's own layout
    calls = [("memory_status", {}),
             ("memory_remember", {"kind": "quirk", "fields": {"description": "x y z"},
                                  "files": [inside]}),
             ("memory_remember", {"kind": "quirk", "fields": {"description": "p q r"},
                                  "files": ["/etc/passwd"]}),
             ("memory_remember", {"kind": "quirk", "fields": {"description": "s t u"},
                                  "files": ["..\..\outside.py"]}),
             ("memory_session_start", {"task": "t"}),
             ("memory_recall", {"task": "anything", "files": ["src/cache.py"]})]
    blob = "
".join(rpc(client, rw, "tools/call", {"name": name, "arguments": args}).text
                     for name, args in calls)
    # a JSON body escapes backslashes: look for the raw, the slash and the escaped form
    variants = {str(tmp_path), tmp_path.as_posix(), json.dumps(str(tmp_path))[1:-1]}
    assert not any(variant in blob for variant in variants)
    assert "path_outside_repo" in blob


def test_status_names_the_store_and_repo_without_their_directories(app_client, tokens):
    client, _ = app_client
    ro = tokens.create("bob", "read-only")
    _, status = tool(client, ro, "memory_status")
    assert status["store"] == "project.memory" and status["repo_root"] == "repo"


def test_host_and_origin_checks(app_client, tokens):
    client, _ = app_client
    token = tokens.create("alice", "read-write")
    assert rpc(client, token, "tools/list", headers={"Host": "evil.example.org"}).status_code == 421
    assert rpc(client, token, "tools/list", headers={"Origin": "http://evil.example.org"}).status_code == 403
    assert rpc(client, token, "tools/list", headers={"Host": "mem.example.com"}).status_code == 200
    assert rpc(client, token, "tools/list", headers={"Host": "localhost:9"}).status_code == 200


def test_the_audit_log_has_names_tools_and_outcomes_but_no_content(app_client, tokens, caplog):
    client, _ = app_client
    rw, ro = tokens.create("alice", "read-write"), tokens.create("bob", "read-only")
    with caplog.at_level(logging.INFO, logger="agent_memory.remote.audit"):
        tool(client, rw, "memory_remember", kind="quirk",
             fields={"description": "SECRET-NOTE-TEXT"}, evidence="SECRET-EVIDENCE")
        tool(client, ro, "memory_remember", kind="quirk", fields={"description": "SECRET-NOTE-TEXT"})
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "name=alice tool=memory_remember outcome=ok" in text
    assert "name=bob tool=memory_remember outcome=forbidden" in text
    assert "SECRET" not in text and rw not in text and ro not in text


def test_a_revoked_tokens_service_is_closed_at_the_next_call(app_client, tokens):
    client, principals = app_client
    a, b = tokens.create("alice", "read-write"), tokens.create("bob", "read-write")
    tool(client, a, "memory_status")
    tool(client, b, "memory_status")
    assert principals.open_count == 2
    tokens.revoke("alice")
    tool(client, b, "memory_status")
    assert principals.open_count == 1


def test_one_service_per_principal_even_when_first_requests_race(base, tokens):
    principals = Principals(base, tokens)
    tokens.create("alice", "read-write")
    entry = tokens.entries()[0]
    principal = Principal("alice", "read-write", entry.created_at)
    barrier = threading.Barrier(6)

    def grab(_):
        barrier.wait()
        return principals.service_for(principal)

    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        services = list(pool.map(grab, range(6)))
    principals.close()
    assert len({id(s) for s in services}) == 1 and principals.open_count == 0


def test_a_read_only_view_has_three_methods_and_no_write_path(base, tokens):
    principals = Principals(base, tokens)
    tokens.create("bob", "read-only")
    view = principals.service_for(Principal("bob", "read-only", tokens.entries()[0].created_at))
    principals.close()
    assert isinstance(view, ReadOnlyView)
    public = {name for name in dir(view) if not name.startswith("_")}
    assert public == {"recall", "check", "status"}


def test_a_closed_principals_refuses_new_services(base, tokens):
    principals = Principals(base, tokens)
    principals.close()
    from agent_memory.service import ServiceError
    with pytest.raises(ServiceError):
        principals.service_for(Principal("x", "read-write", "t"))


@pytest.mark.parametrize("host,expected", [("127.0.0.1", True), ("localhost", True), ("::1", True),
                                           ("[::1]", True), ("127.0.0.5", True), ("0.0.0.0", False),
                                           ("192.168.1.5", False), ("mem.example.com", False), ("", False)])
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


def test_startup_refuses_a_public_bind_without_the_tls_acknowledgement(base, tokens):
    tokens.create("alice", "read-write")
    with pytest.raises(StartupError, match="TLS"):
        check_startup(base, tokens, "0.0.0.0", behind_tls=False)
    check_startup(base, tokens, "0.0.0.0", behind_tls=True)
    check_startup(base, tokens, "127.0.0.1", behind_tls=False)


def test_startup_refuses_when_there_is_no_usable_token(base, tokens):
    with pytest.raises(StartupError, match="amem token create"):
        check_startup(base, tokens, "127.0.0.1", behind_tls=False)
    tokens.create("old", "read-write", expires_in_days=1,
                  now=datetime(2020, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(StartupError):
        check_startup(base, tokens, "127.0.0.1", behind_tls=False)


def test_startup_refuses_a_damaged_token_file_and_a_missing_repo(base, tokens, tmp_path):
    tokens.create("alice", "read-write")
    tokens.path.write_text("{", encoding="utf-8")
    with pytest.raises(StartupError, match="token file"):
        check_startup(base, tokens, "127.0.0.1", behind_tls=False)
    tokens.path.unlink()
    tokens.create("alice", "read-write")
    from dataclasses import replace
    with pytest.raises(StartupError, match="repo"):
        check_startup(replace(base, repo_root=str(tmp_path / "nowhere")), tokens, "127.0.0.1", False)


def test_the_failure_limiter_is_wired_in(base, tokens):
    app, principals = build_app(base, tokens, limiter=FailureLimiter(2, 60.0))
    good = tokens.create("alice", "read-write")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        for _ in range(2):
            assert rpc(client, "amt_bad", "tools/list").status_code == 401
        assert rpc(client, "amt_bad", "tools/list").status_code == 429
        assert rpc(client, good, "tools/list").status_code == 200
    principals.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_remote_app.py -q -p no:cacheprovider`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `remote/server.py`**

```python
"""The HTTP server: one MemoryService per token over a single store, behind the guard.
Imports `mcp`; load it lazily."""
from __future__ import annotations

import ipaddress
import logging
import os
import sys
import threading
from dataclasses import replace
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings

from ..mcp_server import build_server
from ..service import MemoryService, ServiceConfig, ServiceError, path_scrubber
from .limits import FailureLimiter, GuardMiddleware, audit_log, current_principal
from .tokens import Principal, TokenFile

LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]


class StartupError(Exception):
    """The server will not start; the message says why and what to do."""


class ReadOnlyView:
    """What a read-only token can reach: three methods and nothing else."""

    def __init__(self, service: MemoryService):
        self._service = service

    def recall(self, **kwargs: Any) -> Dict[str, Any]:
        return self._service.recall(**kwargs)

    def check(self, **kwargs: Any) -> Dict[str, Any]:
        return self._service.check(**kwargs)

    def status(self) -> Dict[str, Any]:
        return self._service.status()


class Principals:
    """A lazily opened MemoryService per (name, role, creation time) of a token. Each has the
    token's name as its agent, `allow_must` off and paths redacted. A token that left the file
    has its service closed at the next call."""

    def __init__(self, base: ServiceConfig, tokens: TokenFile):
        self._base, self._tokens = base, tokens
        self._lock = threading.Lock()
        self._services: Dict[Tuple[str, str, str], MemoryService] = {}
        self._generation = -1
        self._closed = False

    @property
    def open_count(self) -> int:
        with self._lock:
            return len(self._services)

    def service_for(self, principal: Principal):
        with self._lock:
            if self._closed:
                raise ServiceError("internal", "the server is shutting down")
            self._sweep()
            key = (principal.name, principal.role, principal.created_at)
            service = self._services.get(key)
            if service is None:
                service = MemoryService(replace(self._base, agent=principal.name,
                                                allow_must=False, read_only=False,
                                                redact_paths=True))
                self._services[key] = service
        return ReadOnlyView(service) if principal.role == "read-only" else service

    def _sweep(self) -> None:
        generation = self._tokens.generation
        if generation == self._generation:
            return
        self._generation = generation
        live = {(e.name, e.role, e.created_at) for e in self._tokens.entries()}
        for key in [k for k in self._services if k not in live]:
            self._close_quietly(self._services.pop(key))

    @staticmethod
    def _close_quietly(service: MemoryService) -> None:
        try:
            service.close()
        except Exception:
            logging.getLogger(__name__).exception("a service could not be closed cleanly")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            services, self._services = list(self._services.values()), {}
        for service in services:
            self._close_quietly(service)


def build_app(base: ServiceConfig, tokens: TokenFile, *, public_url: str = "",
              limiter: Optional[FailureLimiter] = None):
    """(asgi_app, principals). The app authenticates every request before anything else."""
    principals = Principals(base, tokens)
    hosts = list(LOOPBACK_HOSTS)
    public_host = urlparse(public_url).netloc if public_url else ""
    if public_host:
        hosts += [public_host] + ([] if ":" in public_host else [public_host + ":*"])

    def provider():
        principal = current_principal.get()
        if principal is None:                          # the guard always sets it: fail closed
            raise ServiceError("forbidden", "no authenticated caller")
        return principals.service_for(principal), principal.role

    def observer(tool: str, outcome: str, ms: int) -> None:
        principal = current_principal.get()
        audit_log.info("call name=%s tool=%s outcome=%s ms=%d",
                       principal.name if principal else "-", tool, outcome, ms)

    server = build_server(
        provider=provider, trust_client_name=False, observer=observer, scrub=path_scrubber(base),
        settings={"stateless_http": True, "json_response": True,
                  "transport_security": TransportSecuritySettings(
                      enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                      allowed_origins=[])})
    return GuardMiddleware(server.streamable_http_app(), tokens, limiter), principals


def is_loopback(host: str) -> bool:
    name = (host or "").strip().strip("[]")
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def check_startup(base: ServiceConfig, tokens: TokenFile, host: str, behind_tls: bool) -> None:
    if not is_loopback(host) and not behind_tls:
        raise StartupError(
            f"binding {host or 'an empty host'} exposes a bearer token over plain HTTP. Put a TLS "
            "proxy or tunnel (Caddy, nginx, Cloudflare Tunnel) in front and add --behind-tls to "
            "say so, or bind 127.0.0.1")
    if tokens.error:
        raise StartupError(f"the token file {tokens.path} is unusable: {tokens.error}")
    if tokens.usable() == 0:
        raise StartupError(f"no usable token in {tokens.path}; run `amem token create NAME "
                           "--role read-write` first (there is no anonymous mode)")
    if not os.path.isdir(base.repo_root):
        raise StartupError(f"the repo root {base.repo_root} is not a directory")


def _configure_audit_log() -> None:
    if audit_log.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    audit_log.addHandler(handler)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False
    logging.getLogger("mcp").setLevel(logging.WARNING)


def run_http(base: ServiceConfig, tokens: TokenFile, host: str, port: int, public_url: str = "",
             behind_tls: bool = False) -> None:
    import uvicorn

    check_startup(base, tokens, host, behind_tls)
    _configure_audit_log()
    app, principals = build_app(base, tokens, public_url=public_url)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                           access_log=False))
    try:
        server.run()
    finally:
        principals.close()                              # each service leaves a checkpoint
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_remote_app.py tests/test_remote_guard.py tests/test_mcp_roles.py -q -p no:cacheprovider`
Expected: all pass. If `test_the_tool_list_is_the_stdio_surface` cannot build the descriptions-only server with `MemoryService.__new__`, construct a real `MemoryService` on a temp repo instead (the test only lists tools).

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/remote/server.py tests/test_remote_app.py
git commit -m "feat: HTTP app with per-token services, read-only view, host checks and startup rules"
```

---

### Task 6: `amem token` and `amem serve --http`

**Files:**
- Create: `src/agent_memory/cli_remote.py`
- Modify: `src/agent_memory/cli_agent.py` (`serve` options)
- Modify: `src/agent_memory/cli.py` (register)
- Test: `tests/test_cli_remote.py`

**Interfaces:**
- Consumes: `TokenFile`, `ROLES`, `TokenError` (Task 2); `run_http`, `StartupError` (Task 5, imported lazily); `resolve_config`.
- Produces: `amem token create NAME --role ROLE [--expires-in DAYS] [--url URL] [--tokens PATH] [--store S] [--repo R] [--json]`, `amem token list [--tokens ...] [--json]`, `amem token revoke NAME [--tokens ...]`; `amem serve --http [--host H] [--port P] [--tokens PATH] [--public-url U] [--behind-tls]`; default token file `<store dir>/tokens.json`, overridable by `--tokens` or `AGENT_MEMORY_TOKENS`.

- [ ] **Step 1: Write the failing tests**

```python
import json
import os

import pytest
from click.testing import CliRunner

from agent_memory.cli import main
from agent_memory.remote.tokens import TokenFile


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def amem(repo):
    runner = CliRunner()

    def invoke(*args, expect=0, json_out=False):
        argv = [*args, "--repo", str(repo)] + (["--json"] if json_out else [])
        result = runner.invoke(main, argv, env={"AGENT_MEMORY_TOKENS": ""})
        assert result.exit_code == expect, result.output
        return json.loads(result.output) if json_out else result.output

    return invoke


def test_create_prints_the_token_once_with_a_paste_ready_snippet(amem, repo):
    out = amem("token", "create", "base44-prod", "--role", "read-write",
               "--url", "https://mem.example.com")
    token = next(word for word in out.split() if word.startswith("amt_"))
    assert "https://mem.example.com/mcp" in out and f"Authorization: Bearer {token}" in out
    assert "shown once" in out
    file = repo / ".agent-memory" / "tokens.json"
    assert token not in file.read_text(encoding="utf-8")
    assert TokenFile(file).verify(token).name == "base44-prod"


def test_create_json_and_list_never_show_a_hash_or_token(amem):
    made = amem("token", "create", "alice", "--role", "read-only", "--expires-in", "30",
                json_out=True)
    assert made["name"] == "alice" and made["role"] == "read-only" and made["token"].startswith("amt_")
    assert made["expires_at"]
    listing = amem("token", "list", json_out=True)
    assert listing["tokens"][0]["name"] == "alice"
    blob = json.dumps(listing)
    assert made["token"] not in blob and "hash" not in blob
    text = amem("token", "list")
    assert "alice" in text and "read-only" in text and made["token"] not in text


def test_create_refuses_a_duplicate_a_bad_name_and_a_bad_role(amem):
    amem("token", "create", "alice", "--role", "read-write")
    assert "already" in amem("token", "create", "alice", "--role", "read-only", expect=2)
    amem("token", "create", "a b", "--role", "read-only", expect=2)
    amem("token", "create", "carol", "--role", "admin", expect=2)


def test_revoke(amem, repo):
    amem("token", "create", "alice", "--role", "read-write")
    assert "revoked alice" in amem("token", "revoke", "alice")
    assert "no token named" in amem("token", "revoke", "alice", expect=2)
    assert TokenFile(repo / ".agent-memory" / "tokens.json").entries() == []


def test_the_tokens_option_and_the_environment_choose_the_file(amem, repo, tmp_path):
    elsewhere = tmp_path / "private" / "t.json"
    amem("token", "create", "alice", "--role", "read-write", "--tokens", str(elsewhere))
    assert elsewhere.exists() and not (repo / ".agent-memory" / "tokens.json").exists()
    runner = CliRunner()
    result = runner.invoke(main, ["token", "list", "--repo", str(repo)],
                           env={"AGENT_MEMORY_TOKENS": str(elsewhere)})
    assert result.exit_code == 0 and "alice" in result.output


def test_a_damaged_file_is_reported_not_overwritten(amem, repo):
    path = repo / ".agent-memory" / "tokens.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    out = amem("token", "create", "alice", "--role", "read-write", expect=2)
    assert "fix or remove" in out and path.read_text(encoding="utf-8") == "{"


def test_serve_http_refuses_to_start_with_no_token(amem):
    out = amem("serve", "--http", expect=2)
    assert "amem token create" in out


def test_serve_http_refuses_a_public_bind_without_the_tls_flag(amem):
    amem("token", "create", "alice", "--role", "read-write")
    out = amem("serve", "--http", "--host", "0.0.0.0", expect=2)
    assert "--behind-tls" in out


def test_serve_http_refuses_stdio_only_options(amem):
    amem("token", "create", "alice", "--role", "read-write")
    out = amem("serve", "--http", "--allow-must", expect=2)
    assert "allow-must" in out
    out = amem("serve", "--agent", "x", "--http", expect=2)
    assert "agent" in out


def test_http_only_options_need_http(amem):
    out = amem("serve", "--port", "9000", expect=2)
    assert "--http" in out
```
(The last two tests run `serve` with `--repo` appended by the helper; `serve` already accepts `--repo`.)

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli_remote.py -q -p no:cacheprovider`
Expected: FAIL (no `token` command).

- [ ] **Step 3: Implement**

`cli_remote.py`:

```python
"""`amem token ...` (manage bearer tokens) and the helper behind `amem serve --http`."""
from __future__ import annotations

import json
import os
import sys
from typing import Optional

import click

from .cli_agent import _emit, _fail
from .remote.tokens import ROLES, TokenError, TokenFile
from .service import ServiceError, resolve_config


def _tokens_path(tokens: Optional[str], store: Optional[str], repo: Optional[str]) -> str:
    chosen = tokens or os.environ.get("AGENT_MEMORY_TOKENS")
    if chosen:
        return os.path.abspath(chosen)
    config = resolve_config(store=store, repo=repo)
    return os.path.join(os.path.dirname(config.store_path), "tokens.json")


def _options(fn):
    for option in reversed([
        click.option("--tokens", default=None, help="Token file (default: tokens.json beside "
                     "the store, or $AGENT_MEMORY_TOKENS)."),
        click.option("--store", default=None, help="Memory store path (locates tokens.json)."),
        click.option("--repo", default=None, help="Repository root (locates the store)."),
        click.option("--json", "as_json", is_flag=True, help="Machine-readable output."),
    ]):
        fn = option(fn)
    return fn


def _die(message: str, as_json: bool) -> None:
    _fail(ServiceError("invalid_argument", message), as_json)


@click.group("token")
def token_group():
    """Create, list and revoke the bearer tokens of the remote server."""


@token_group.command("create")
@click.argument("name")
@click.option("--role", type=click.Choice(ROLES), required=True,
              help="read-only: recall, check, status. read-write: everything an agent may do.")
@click.option("--expires-in", type=int, default=None, help="Days until the token stops working.")
@click.option("--url", default="http://127.0.0.1:8765", show_default=True,
              help="The server's public URL, for the snippet.")
@_options
def token_create(name, role, expires_in, url, tokens, store, repo, as_json):
    """Make a token. It is shown once; only its hash is kept."""
    file = TokenFile(_tokens_path(tokens, store, repo))
    try:
        token = file.create(name, role, expires_in_days=expires_in)
    except TokenError as exc:
        _die(str(exc), as_json)
        return
    entry = next(e for e in file.entries() if e.name == name)
    if as_json:
        click.echo(json.dumps({"name": name, "role": role, "token": token,
                               "expires_at": entry.expires_at, "file": str(file.path)}, indent=2))
        return
    endpoint = url.rstrip("/") + "/mcp"
    _emit(f"Token for '{name}' ({role}), shown once - store it now:\n\n  {token}\n\n"
          f"MCP server URL:  {endpoint}\n"
          f"Header:          Authorization: Bearer {token}\n"
          f"Expires:         {entry.expires_at or 'never'}\n"
          f"Token file:      {file.path}")


@token_group.command("list")
@_options
def token_list(tokens, store, repo, as_json):
    """List tokens (name, role, created, expiry; never a token or its hash)."""
    file = TokenFile(_tokens_path(tokens, store, repo))
    if file.error:
        _die(file.error, as_json)
        return
    rows = [{"name": e.name, "role": e.role, "created_at": e.created_at,
             "expires_at": e.expires_at} for e in file.entries()]
    if as_json:
        click.echo(json.dumps({"tokens": rows}, indent=2))
        return
    if not rows:
        _emit("no tokens")
        return
    for row in rows:
        _emit(f"{row['name']}  {row['role']}  created {row['created_at']}  "
              f"expires {row['expires_at'] or 'never'}")


@token_group.command("revoke")
@click.argument("name")
@_options
def token_revoke(name, tokens, store, repo, as_json):
    """Stop a token from working (a running server notices on its next request)."""
    file = TokenFile(_tokens_path(tokens, store, repo))
    try:
        removed = file.revoke(name)
    except TokenError as exc:
        _die(str(exc), as_json)
        return
    if not removed:
        _die(f"no token named {name!r}", as_json)
        return
    _emit(f"revoked {name}")


def serve_http(store, repo, host, port, tokens, public_url, behind_tls) -> None:
    try:
        from .remote.server import StartupError, run_http
    except ImportError as exc:
        click.echo(f"the remote server needs the optional 'mcp' package (Python 3.10+): {exc}",
                   err=True)
        sys.exit(1)
    config = resolve_config(store=store, repo=repo, agent="remote", allow_must=False)
    file = TokenFile(_tokens_path(tokens, store, repo))
    try:
        run_http(config, file, host or "127.0.0.1", port or 8765, public_url or "", behind_tls)
    except StartupError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)


COMMANDS = [token_group]


def register(group: click.Group) -> None:
    for command in COMMANDS:
        group.add_command(command)
```

`cli_agent.py`: replace the `serve` command with:

```python
@click.command("serve")
@click.option("--store", default=None, help="Memory store path.")
@click.option("--repo", default=None, help="Repository root.")
@click.option("--agent", default=None, help="Default agent name (an MCP client's own name wins).")
@click.option("--allow-must", is_flag=True, help="Let agents record priority 'must' notes.")
@click.option("--http", "http", is_flag=True,
              help="Serve over streamable HTTP with bearer tokens (see `amem token`).")
@click.option("--host", default=None, help="With --http: bind address (default 127.0.0.1).")
@click.option("--port", default=None, type=int, help="With --http: port (default 8765).")
@click.option("--tokens", default=None, help="With --http: the token file.")
@click.option("--public-url", default=None, help="With --http: the URL clients use (allows its host).")
@click.option("--behind-tls", is_flag=True,
              help="With --http: a TLS proxy or tunnel fronts this server (needed to bind publicly).")
def serve(store, repo, agent, allow_must, http, host, port, tokens, public_url, behind_tls):
    """Serve the memory over MCP: stdio (started by an MCP client), or HTTP with --http."""
    if http:
        if agent or allow_must:
            _fail(ServiceError("invalid_argument", "with --http the agent name comes from each "
                               "token and --allow-must is never available to remote callers; "
                               "drop --agent and --allow-must"), False)
        from .cli_remote import serve_http
        serve_http(store, repo, host, port, tokens, public_url, behind_tls)
        return
    if host or port or tokens or public_url or behind_tls:
        _fail(ServiceError("invalid_argument", "--host, --port, --tokens, --public-url and "
                           "--behind-tls need --http"), False)
    try:
        from .mcp_server import serve as run_server
    except ImportError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    try:
        run_server(resolve_config(store=store, repo=repo, agent=agent, allow_must=allow_must))
    except ServiceError as exc:                  # e.g. a store the service refuses at startup
        _fail(exc, False)
```

`cli.py`: change the import and registration lines to include `cli_remote`:

```python
from . import cli_agent, cli_remote, cli_review  # noqa: E402
...
cli_remote.register(main)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_cli_remote.py tests/test_cli_review.py tests/test_cli_agent.py -q -p no:cacheprovider`
Expected: all pass. (`amem serve` over stdio still works: `tests/test_mcp_server.py` starts it as a subprocess.)

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/cli_remote.py src/agent_memory/cli_agent.py src/agent_memory/cli.py tests/test_cli_remote.py
git commit -m "feat: amem token commands and amem serve --http"
```

---

### Task 7: End to end, documentation, corrections

**Files:**
- Test: `tests/test_remote_e2e.py`
- Modify: `README.md` (new "Remote server" section)
- Modify: `docs/superpowers/specs/2026-11-20-remote-mcp-design.md` (corrections and status)
- Modify: `docs/superpowers/specs/2026-11-17-agent-surface-design.md` (status line for 3b)

**Interfaces:**
- Consumes: everything above.
- Produces: tests that run a real `uvicorn` server in a thread with a real MCP client, and a subprocess `amem serve --http`; documentation; spec corrections.

- [ ] **Step 1: Write the end-to-end tests**

```python
import asyncio
import json
import socket
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("mcp")
import uvicorn                                                        # noqa: E402
from mcp import ClientSession, types                                  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client      # noqa: E402
from mcp.client.streamable_http import streamablehttp_client          # noqa: E402

from agent_memory.remote.server import build_app                      # noqa: E402
from agent_memory.remote.tokens import TokenFile                      # noqa: E402
from agent_memory.service import resolve_config                       # noqa: E402
from storage_helpers import SRC, child_env                            # noqa: E402


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live(repo, tmp_path):
    base = resolve_config(repo=str(repo), agent="remote", env={})
    tokens = TokenFile(tmp_path / "tokens.json")
    app, principals = build_app(base, tokens)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}/mcp", tokens, base
    server.should_exit = True
    thread.join(10)
    principals.close()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 90))


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    return result.isError, result.content[0].text


def test_a_real_client_writes_with_one_token_and_reads_with_another(live):
    url, tokens, _ = live
    rw, ro = tokens.create("alice", "read-write"), tokens.create("bob", "read-only")

    async def scenario():
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {rw}"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()
                names = [t.name for t in (await client.list_tools()).tools]
                made = await call(client, "memory_remember", kind="quirk",
                                  fields={"description": "the zorblax queue drops items over 64KB"},
                                  files=["src/cache.py"])
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {ro}"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()
                found = await call(client, "memory_recall", task="zorblax queue drops items")
                refused = await call(client, "memory_retract", id=json.loads(made[1])["id"])
        return names, made, found, refused

    names, made, found, refused = run(scenario())
    assert len(names) == 11
    assert not made[0] and json.loads(made[1])["created"] is True
    assert not found[0] and json.loads(found[1])["items"][0]["id"] == json.loads(made[1])["id"]
    assert refused[0] and "forbidden" in refused[1]


def test_a_wrong_token_never_gets_a_session(live):
    url, tokens, _ = live
    tokens.create("alice", "read-write")

    async def scenario():
        async with streamablehttp_client(url, headers={"Authorization": "Bearer amt_nope"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()

    with pytest.raises(BaseException):
        run(scenario())


def test_revoking_a_token_cuts_a_running_server_off_without_a_restart(live):
    url, tokens, _ = live
    token = tokens.create("alice", "read-write")

    async def attempt():
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()
                return await call(client, "memory_status")

    assert not run(attempt())[0]
    tokens.revoke("alice")
    with pytest.raises(BaseException):
        run(attempt())


def test_a_stdio_server_and_the_http_server_share_one_store(live, repo):
    url, tokens, base = live
    token = tokens.create("alice", "read-write")
    params = StdioServerParameters(command=sys.executable, args=["-m", "agent_memory.cli", "serve",
                                   "--repo", str(repo)], env=child_env())

    async def scenario():
        async with stdio_client(params) as (sr, sw):
            async with ClientSession(sr, sw, client_info=types.Implementation(name="claude-code", version="1")) as local:
                await local.initialize()
                await call(local, "memory_remember", kind="quirk",
                           fields={"description": "written over stdio, read over http"})
                async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
                    async with ClientSession(r, w) as remote:
                        await remote.initialize()
                        seen = await call(remote, "memory_recall", task="written over stdio read over http")
                        await call(remote, "memory_remember", kind="quirk",
                                   fields={"description": "written over http, read over stdio"})
                back = await call(local, "memory_recall", task="written over http read over stdio")
                return seen, back

    seen, back = run(scenario())
    assert json.loads(seen[1])["items"] and json.loads(back[1])["items"]


def test_amem_serve_http_runs_as_a_process_and_stops_cleanly(repo, tmp_path):
    from agent_memory.remote.tokens import TokenFile as TF
    tokens = TF(tmp_path / "tokens.json")
    token = tokens.create("alice", "read-write")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_memory.cli", "serve", "--http", "--port", str(port),
         "--repo", str(repo), "--tokens", str(tokens.path)],
        env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)

        async def scenario():
            async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
                async with ClientSession(r, w) as client:
                    await client.initialize()
                    return await call(client, "memory_status")

        is_error, text = run(scenario())
        assert not is_error and json.loads(text)["agent"] == "alice"
    finally:
        proc.terminate()
        try:
            _, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
    assert token not in err and "Traceback" not in err
    assert "name=alice tool=memory_status outcome=ok" in err
    assert (repo / ".agent-memory" / "project.memory").exists()      # left a snapshot
```
If a `terminate()` on Windows kills the child before `finally` runs (so no checkpoint and no clean stderr), keep the audit-line and no-token assertions and drop only the snapshot assertion, recording that in the PR description.

- [ ] **Step 2: Run the end-to-end tests**

Run: `python -m pytest tests/test_remote_e2e.py -q -p no:cacheprovider`
Expected: all pass. Fix any defect they reveal in the earlier tasks' code (with its own test) before continuing.

- [ ] **Step 3: Documentation**

README: add a "Remote server" section after "Lessons and review" containing:
- when to use it (hosted agents that need a URL), the requirement for a checkout on the server, and the install line `pip install 'agent-memory[mcp]'`;
- the commands: `amem token create base44-prod --role read-write --url https://memory.example.com`, `amem token list`, `amem token revoke`, `amem serve --http --public-url https://memory.example.com --behind-tls` (binding loopback by default);
- the role table (read-only: `memory_recall`, `memory_check`, `memory_status`; read-write: all eleven);
- a Caddy example (`memory.example.com { reverse_proxy 127.0.0.1:8765 }`) and a Cloudflare Tunnel example (`cloudflared tunnel --url http://127.0.0.1:8765`);
- Base44: add a custom MCP by URL `https://memory.example.com/mcp` with the header `Authorization: Bearer <token>`; Devin: unverified, same URL and header;
- the audit log format (`call name=... tool=... outcome=... ms=...`, `auth denied reason=...`);
- what a remote agent cannot do (accept lessons, `must`, choose the repo or store) and the known limits: tokens expire only at request time and a reload of the file closes a revoked token's service; the failure limiter is global; the token file mode is best effort on Windows; one store and one repo per server; HTTP is plain, so a proxy is required off loopback.

Spec (`2026-11-20-remote-mcp-design.md`): update the Status line to "implemented", correct section 5 (Origin not allowed is 403, Host not allowed is 421) and section 7's table, replace section 3's "uses the SDK" wording and section 12 with a "Findings from the spike" list: own ASGI guard instead of the SDK's OAuth-shaped verifier (no OAuth metadata route, a fixed 401, one place for limits); identity passes through a context variable that FastMCP's stateless mode propagates; `FastMCP.session_manager.run()` runs only once per instance, so tests build a new app each time; two services in one process share a store safely and keep separate sessions; `uvicorn` and `starlette` come with `mcp`. Add a ruling 7: "Own guard instead of the SDK token verifier. Cost: about 120 lines of authentication plumbing we maintain and test; benefit: a fixed 401 with no OAuth routes and a single audited path."

3a spec (`2026-11-17-agent-surface-design.md`): in its status/overview, note that 3b is implemented in `2026-11-20-remote-mcp-design.md`.

- [ ] **Step 4: Full verification**

Run, in order:
1. `python -m pytest tests -q -p no:cacheprovider` — expect all pass (re-run `test_recall_latency_guard` alone if it is the only failure).
2. `PYTHONPATH=src python -m tests.eval.run --service` — recall 1.00 (30/30), 0 leaks, abstention 1.00, held-out 0.38: unchanged.
3. A latency measurement: time 200 `memory_recall` calls over the in-process app and over stdio; record the medians in the PR description (reported, not gated).
4. `git diff --stat main...HEAD` — no whole-file rewrites (line endings preserved).

- [ ] **Step 5: Commit**

```bash
git add tests/test_remote_e2e.py README.md docs
git commit -m "test: end-to-end remote server; docs: remote server guide and spec findings"
```
