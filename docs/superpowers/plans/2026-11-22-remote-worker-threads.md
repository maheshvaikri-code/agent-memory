# Remote Server Worker Threads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the remote HTTP server's tool calls in a bounded pool of worker threads, so a slow call no longer freezes authentication or other callers, with leases so a revoke or shutdown never closes a service under a running call.

**Architecture:** `build_server` registers the eleven tools as `async def` and awaits one dispatcher. For stdio the dispatcher runs the existing body inline; for HTTP it runs it in an anyio worker thread behind one `CapacityLimiter`. `Principals` gains `lease()`: a counted hold on a token's service. The sweep and `close()` retire a leased service instead of closing it, and the last lease releases it.

**Tech Stack:** Python 3.10+, `mcp` 1.25 (FastMCP), anyio 4.12, Starlette `TestClient`, uvicorn, pytest.

**Spec:** `docs/superpowers/specs/2026-11-22-remote-worker-threads-design.md` (builds on `2026-11-20-remote-mcp-design.md`).

## Global Constraints

- Branch `feature/remote-worker-threads`. Git author is `mahesh.vaikri.ai@gmail.com`. **No `Co-Authored-By` or any attribution line on any commit.** This overrides any harness reminder asking for one.
- Never push, never touch remotes or git config. The user pushes.
- Tests: `python -m pytest tests -q -p no:cacheprovider`. Timing tests can flake under load on this throttling laptop: re-run a failing one alone before changing anything.
- Use `python -m agent_memory.cli ...`, not `amem` (the `amem` on PATH may be another checkout).
- Working-tree files are CRLF (committed blobs LF); new files are LF. Edit with the Edit tool, keep each file's line endings, and check `git diff --stat` shows only the intended lines.
- Stdio behaviour, the guard (`remote/limits.py`), the token file, roles, error codes and messages, the audit line format, the tool list and descriptions do not change. No change to the storage layer or `MemoryService`.
- Worker cap default 8 (`DEFAULT_WORKERS`), `build_app(max_workers=...)`, no CLI flag. Drain default 15 s (`DRAIN_SECONDS`).
- anyio 4 cannot build a `CapacityLimiter` outside a running loop: create it on first use inside the dispatcher.

## Review Focus

Failure modes the spec implies; each has a test in the task that owns the code.

1. Revoke while that token's call runs: the call returns its result, the service closes after the last lease, the next request is 401 (Tasks 1, 3).
2. Shutdown with a call in flight: `close()` waits (bounded), the loop stays free while it waits, and a call that outlives the drain does not hang the process (Tasks 1, 3).
3. A call that raises, a read-only caller refused with `forbidden`, or a request cancelled mid-call must still release its lease (Tasks 2, 3).
4. More calls than workers: they queue, authentication still answers at once, and the audit `ms` includes the queue wait (Tasks 2, 3).
5. The token identity must survive the thread hop: the note's agent is the token name, not the client's claim (Task 3, existing test plus the thread assertion).

---

### Task 1: Leases and drain in `Principals`

**Files:**
- Modify: `src/agent_memory/remote/server.py` (the `Principals` class and imports)
- Create: `tests/test_remote_leases.py`

**Interfaces:**
- Consumes: `MemoryService(config)` with `.close()` (idempotent) and a private `_closed` flag; `TokenFile.generation`, `.error`, `.entries()`; `Principal(name, role, created_at)`; `ServiceError`.
- Produces: `Principals.lease(principal)`, a context manager yielding `(target, role)` where `target` is the service (read-write) or a `ReadOnlyView` (read-only); `Principals.close(drain_seconds: float = DRAIN_SECONDS)`; properties `open_count` (active services), `retired_count` (departed-token services still leased), `leases_held` (all leases); `service_for(principal)` unchanged in behaviour; module constant `DRAIN_SECONDS = 15.0`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_remote_leases.py`:

```python
import logging
import threading

import pytest

pytest.importorskip("mcp")

from agent_memory.remote.server import Principals, ReadOnlyView      # noqa: E402
from agent_memory.remote.tokens import Principal, TokenFile          # noqa: E402
from agent_memory.service import ServiceError, resolve_config        # noqa: E402


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


def principal(tokens, name, role="read-write"):
    tokens.create(name, role)
    entry = next(e for e in tokens.entries() if e.name == name)
    return Principal(entry.name, entry.role, entry.created_at)


def test_a_lease_yields_the_service_and_the_role_and_counts_while_held(base, tokens):
    principals = Principals(base, tokens)
    alice = principal(tokens, "alice")
    with principals.lease(alice) as (service, role):
        assert role == "read-write" and not isinstance(service, ReadOnlyView)
        assert principals.leases_held == 1 and principals.open_count == 1
        with principals.lease(alice) as (again, _):
            assert again is service and principals.leases_held == 2
    assert principals.leases_held == 0
    principals.close()


def test_a_read_only_principal_leases_the_three_method_view(base, tokens):
    principals = Principals(base, tokens)
    bob = principal(tokens, "bob", "read-only")
    with principals.lease(bob) as (view, role):
        assert isinstance(view, ReadOnlyView) and role == "read-only"
    principals.close()


def test_a_lease_is_released_when_the_body_raises(base, tokens):
    principals = Principals(base, tokens)
    alice = principal(tokens, "alice")
    with pytest.raises(RuntimeError):
        with principals.lease(alice):
            raise RuntimeError("boom")
    assert principals.leases_held == 0
    principals.close()


def test_a_sweep_retires_a_leased_service_and_the_last_release_closes_it(base, tokens):
    principals = Principals(base, tokens)
    alice, bob = principal(tokens, "alice"), principal(tokens, "bob")
    with principals.lease(alice) as (service, _):
        tokens.revoke("alice")
        principals.service_for(bob)                       # the next call sweeps
        assert principals.open_count == 1 and principals.retired_count == 1
        assert not service._closed                        # a call is using it
    assert service._closed and principals.retired_count == 0
    principals.close()


def test_a_sweep_still_closes_an_idle_revoked_service_at_once(base, tokens):
    principals = Principals(base, tokens)
    alice, bob = principal(tokens, "alice"), principal(tokens, "bob")
    service = principals.service_for(alice)
    tokens.revoke("alice")
    principals.service_for(bob)
    assert service._closed and principals.retired_count == 0
    principals.close()


def test_close_waits_for_a_running_call_then_closes_everything(base, tokens):
    principals = Principals(base, tokens)
    alice = principal(tokens, "alice")
    holding, release, seen = threading.Event(), threading.Event(), {}

    def call():
        with principals.lease(alice) as (service, _):
            seen["service"] = service
            holding.set()
            release.wait(10)

    worker = threading.Thread(target=call)
    worker.start()
    assert holding.wait(5)
    closer = threading.Thread(target=principals.close, kwargs={"drain_seconds": 10})
    closer.start()
    closer.join(0.4)
    assert closer.is_alive()                              # waiting for the call
    assert not seen["service"]._closed
    release.set()
    worker.join(5)
    closer.join(5)
    assert not closer.is_alive() and seen["service"]._closed
    assert principals.open_count == 0 and principals.leases_held == 0


def test_close_gives_up_after_the_drain_time_and_says_so(base, tokens, caplog):
    principals = Principals(base, tokens)
    alice = principal(tokens, "alice")
    holding, release, seen = threading.Event(), threading.Event(), {}

    def call():
        with principals.lease(alice) as (service, _):
            seen["service"] = service
            holding.set()
            release.wait(10)

    worker = threading.Thread(target=call)
    worker.start()
    assert holding.wait(5)
    with caplog.at_level(logging.WARNING, logger="agent_memory.remote.server"):
        principals.close(drain_seconds=0.3)
    assert seen["service"]._closed
    assert any("still running" in record.getMessage() for record in caplog.records)
    release.set()
    worker.join(5)                                        # the late release must not raise
    assert principals.leases_held == 0


def test_a_closed_principals_refuses_new_leases(base, tokens):
    principals = Principals(base, tokens)
    principals.close()
    with pytest.raises(ServiceError):
        with principals.lease(Principal("x", "read-write", "t")):
            pass


def test_close_is_idempotent_after_a_drain(base, tokens):
    principals = Principals(base, tokens)
    alice = principal(tokens, "alice")
    with principals.lease(alice):
        pass
    principals.close(drain_seconds=1)
    principals.close(drain_seconds=1)
    assert principals.open_count == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_remote_leases.py -q -p no:cacheprovider`
Expected: FAIL (`AttributeError: 'Principals' object has no attribute 'lease'`, and `leases_held`).

- [ ] **Step 3: Implement**

In `src/agent_memory/remote/server.py`, add `import contextlib` to the imports (after `import ipaddress` is fine) and replace the whole `Principals` class (from `class Principals:` to just before `class CloseOnShutdown:`) with:

```python
DRAIN_SECONDS = 15.0


class _Entry:
    """A cached service, how many calls hold it open, and whether its token has gone."""
    __slots__ = ("service", "leases", "retired")

    def __init__(self, service: MemoryService):
        self.service, self.leases, self.retired = service, 0, False


class Principals:
    """A lazily opened MemoryService per (name, role, creation time) of a token. Each has the
    token's name as its agent, `allow_must` off and paths redacted. A call holds a lease on its
    service for its whole duration. A token that left the file has its service closed at the next
    call, or, if a call is still using it, when that call's lease ends."""

    def __init__(self, base: ServiceConfig, tokens: TokenFile):
        self._base, self._tokens = base, tokens
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)    # signalled when a retired service is released
        self._services: Dict[Tuple[str, str, str], _Entry] = {}
        self._retired: List[_Entry] = []                   # departed tokens' services still in use
        self._generation = -1
        self._closed = False

    @property
    def open_count(self) -> int:
        with self._lock:
            return len(self._services)

    @property
    def retired_count(self) -> int:
        with self._lock:
            return len(self._retired)

    @property
    def leases_held(self) -> int:
        with self._lock:
            return sum(e.leases for e in self._services.values()) + \
                sum(e.leases for e in self._retired)

    def _entry_for(self, principal: Principal) -> _Entry:
        """The caller holds the lock."""
        if self._closed:
            raise ServiceError("internal", "the server is shutting down")
        self._sweep()
        key = (principal.name, principal.role, principal.created_at)
        entry = self._services.get(key)
        if entry is None:
            entry = _Entry(MemoryService(replace(self._base, agent=principal.name,
                                                 allow_must=False, read_only=False,
                                                 redact_paths=True)))
            self._services[key] = entry
        return entry

    @staticmethod
    def _view(principal: Principal, service: MemoryService):
        return ReadOnlyView(service) if principal.role == "read-only" else service

    def service_for(self, principal: Principal):
        """The service (or read-only view) without holding it open. Tests and callers that do not
        run a call use this; a call uses `lease`."""
        with self._lock:
            entry = self._entry_for(principal)
        return self._view(principal, entry.service)

    @contextlib.contextmanager
    def lease(self, principal: Principal):
        """Hold the principal's service open for the duration of the `with` block; yields
        (service or read-only view, role). Released on success, error or cancellation."""
        with self._lock:
            entry = self._entry_for(principal)
            entry.leases += 1
        try:
            yield self._view(principal, entry.service), principal.role
        finally:
            self._release(entry)

    def _release(self, entry: _Entry) -> None:
        with self._lock:
            entry.leases -= 1
            last = entry.retired and entry.leases == 0 and entry in self._retired
        if not last:
            return
        self._close_quietly(entry.service)                 # outside the lock: a close can take a moment
        with self._changed:
            if entry in self._retired:
                self._retired.remove(entry)
            self._changed.notify_all()

    def _sweep(self) -> None:
        """Close the services of tokens that left the file, but only on a consistent, clean
        snapshot: no error before the read and the same generation around it (every refresh
        that changes anything bumps it, so the entries came from one clean read). Otherwise do
        nothing and do not record the generation, so the next call retries. An outage lists no
        tokens; closing then would lose every session (requests still fail closed at the guard).
        A departed service with a call running is retired, not closed: its last lease closes it."""
        before = self._tokens.generation
        if self._tokens.error:
            return
        entries = self._tokens.entries()
        if self._tokens.generation != before or before == self._generation:
            return                         # the file changed or failed meanwhile, or nothing new
        self._generation = before
        live = {(e.name, e.role, e.created_at) for e in entries}
        for key in [k for k in self._services if k not in live]:
            entry = self._services.pop(key)
            if entry.leases == 0:
                self._close_quietly(entry.service)
            else:
                entry.retired = True
                self._retired.append(entry)

    @staticmethod
    def _close_quietly(service: MemoryService) -> None:
        try:
            service.close()
        except Exception:
            logging.getLogger(__name__).exception("a service could not be closed cleanly")

    def close(self, drain_seconds: float = DRAIN_SECONDS) -> None:
        """Refuse new leases, close every idle service, wait up to `drain_seconds` for running
        calls to finish (their last lease closes their service), then close what is left and
        say so. Closing a service still takes its own lock, so a forced close waits for a call
        inside a method, which the store's lock timeout bounds. Idempotent."""
        with self._lock:
            self._closed = True
            for entry in self._services.values():
                entry.retired = True
                self._retired.append(entry)
            self._services = {}
            idle = [e for e in self._retired if e.leases == 0]
            self._retired = [e for e in self._retired if e.leases > 0]
        for entry in idle:
            self._close_quietly(entry.service)
        deadline = time.monotonic() + drain_seconds
        with self._changed:
            while self._retired:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            stuck, self._retired = self._retired, []
        if stuck:
            logging.getLogger(__name__).warning(
                "closing %d service(s) with calls still running", len(stuck))
        for entry in stuck:
            self._close_quietly(entry.service)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_remote_leases.py tests/test_remote_app.py tests/test_remote_guard.py tests/test_remote_tokens.py -q -p no:cacheprovider`
Expected: all pass (the existing `Principals` tests still use `service_for` and `open_count`).

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/remote/server.py tests/test_remote_leases.py
git commit -m "feat: lease a token's service for the duration of a call"
```

---

### Task 2: Async tools and the worker dispatcher in `build_server`

**Files:**
- Modify: `src/agent_memory/mcp_server.py` (imports, `build_server`, the eleven tool definitions)
- Create: `tests/test_mcp_workers.py`

**Interfaces:**
- Consumes: `build_server(service=None, *, provider=None, trust_client_name=None, observer=None, scrub=None, settings=None)` as it is today; `READ_ONLY_TOOLS`, `TOOL_METHODS`.
- Produces: `build_server(service=None, *, provider=None, lease=None, workers=None, trust_client_name=None, observer=None, scrub=None, settings=None)`. Exactly one of `service`, `provider`, `lease` (else `ValueError`). `lease` is a zero-argument callable returning a context manager that yields `(target, role)`. `workers` is `None` (run inline on the loop, as stdio does today) or an int >= 1 (run each call in a worker thread, at most that many at once). Audit `ms` is measured from when the call is accepted, so it includes the queue wait.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_workers.py`:

```python
import asyncio
import contextlib
import json
import threading
import time

import pytest

pytest.importorskip("mcp")

from mcp.shared.memory import create_connected_server_and_client_session   # noqa: E402

from agent_memory.mcp_server import TOOL_NAMES, build_server                # noqa: E402
from agent_memory.service import MemoryService, resolve_config              # noqa: E402


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def service(repo):
    made = MemoryService(resolve_config(repo=str(repo), agent="w", env={}))
    yield made
    made.close()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    return result.isError, result.content[0].text


class CountingLease:
    """A lease double: counts entries and exits, yields the one service."""

    def __init__(self, service, role="read-write"):
        self.service, self.role = service, role
        self.entered = self.exited = 0
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def __call__(self):
        with self._lock:
            self.entered += 1
        try:
            yield self.service, self.role
        finally:
            with self._lock:
                self.exited += 1


def test_every_tool_is_registered_as_a_coroutine(service):
    server = build_server(service)
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    assert set(tools) == set(TOOL_NAMES)
    assert all(t.is_async for t in tools.values())


def test_exactly_one_of_service_provider_and_lease_is_required(service):
    lease = CountingLease(service)
    with pytest.raises(ValueError):
        build_server()
    with pytest.raises(ValueError):
        build_server(service, lease=lease)
    with pytest.raises(ValueError):
        build_server(provider=lambda: (service, "read-write"), lease=lease)
    with pytest.raises(ValueError):
        build_server(lease=lease, workers=0)


def test_a_plain_service_still_runs_on_the_loop_thread(service, monkeypatch):
    seen = {}
    original = MemoryService.recall

    def spy(self, *args, **kwargs):
        seen["thread"] = threading.get_ident()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "recall", spy)

    async def scenario():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            await call(client, "memory_recall", task="anything")
        return threading.get_ident()

    loop_thread = run(scenario())
    assert seen["thread"] == loop_thread


def test_workers_run_the_call_on_another_thread_through_the_lease(service, monkeypatch):
    seen, lease = {}, CountingLease(service)
    original = MemoryService.recall

    def spy(self, *args, **kwargs):
        seen["thread"] = threading.get_ident()
        seen["held"] = (lease.entered, lease.exited)       # the lease is held while the method runs
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "recall", spy)

    async def scenario():
        server = build_server(lease=lease, workers=2)
        async with create_connected_server_and_client_session(server) as client:
            result = await call(client, "memory_recall", task="anything")
        return threading.get_ident(), result

    loop_thread, (is_error, _text) = run(scenario())
    assert not is_error
    assert seen["thread"] != loop_thread
    assert seen["held"] == (1, 0) and (lease.entered, lease.exited) == (1, 1)


def test_a_slow_call_does_not_block_the_event_loop(service, monkeypatch):
    release = threading.Event()
    original = MemoryService.recall

    def slow(self, *args, **kwargs):
        release.wait(10)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "recall", slow)

    async def scenario():
        server = build_server(lease=CountingLease(service), workers=2)
        ticks = 0
        async with create_connected_server_and_client_session(server) as client:
            timer = threading.Timer(1.0, release.set)
            timer.start()
            pending = asyncio.ensure_future(call(client, "memory_recall", task="x"))
            while not pending.done():
                await asyncio.sleep(0.05)
                ticks += 1
            result = await pending
        timer.cancel()
        return ticks, result

    ticks, (is_error, _text) = run(scenario())
    release.set()
    assert not is_error and ticks >= 10                    # inline, the loop would tick about once


def test_the_worker_cap_limits_how_many_calls_run_at_once(service, monkeypatch):
    running, peak, lock = [0], [0], threading.Lock()
    original = MemoryService.recall

    def counted(self, *args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.2)
        try:
            return original(self, *args, **kwargs)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(MemoryService, "recall", counted)

    async def scenario():
        server = build_server(lease=CountingLease(service), workers=2)
        async with create_connected_server_and_client_session(server) as client:
            return await asyncio.gather(*[call(client, "memory_recall", task=f"t{i}")
                                          for i in range(5)])

    results = run(scenario())
    assert all(not is_error for is_error, _ in results)
    assert peak[0] == 2                                    # reached the cap and never passed it


def test_the_audit_duration_includes_the_wait_in_the_queue(service, monkeypatch):
    original = MemoryService.recall
    seen = []

    def slow(self, *args, **kwargs):
        time.sleep(0.3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "recall", slow)

    async def scenario():
        server = build_server(lease=CountingLease(service), workers=1,
                              observer=lambda tool, outcome, ms: seen.append(ms))
        async with create_connected_server_and_client_session(server) as client:
            await asyncio.gather(call(client, "memory_recall", task="a"),
                                 call(client, "memory_recall", task="b"))

    run(scenario())
    assert len(seen) == 2 and min(seen) >= 250 and max(seen) >= 500


def test_the_lease_is_released_when_the_service_refuses(service):
    lease = CountingLease(service)

    async def scenario():
        async with create_connected_server_and_client_session(
                build_server(lease=lease, workers=2)) as client:
            return await call(client, "memory_retract", id="no-such-note")

    is_error, text = run(scenario())
    assert is_error and json.loads(text)["error"]["code"] == "not_found"
    assert (lease.entered, lease.exited) == (1, 1)


def test_the_lease_is_released_when_the_role_is_refused(service):
    lease = CountingLease(service, role="read-only")

    async def scenario():
        async with create_connected_server_and_client_session(
                build_server(lease=lease, workers=2)) as client:
            return await call(client, "memory_remember", kind="quirk", fields={"description": "d"})

    is_error, text = run(scenario())
    assert is_error and json.loads(text)["error"]["code"] == "forbidden"
    assert (lease.entered, lease.exited) == (1, 1)


def test_the_lease_is_released_whichever_way_a_cancelled_call_ends(service, monkeypatch):
    release, entered = threading.Event(), threading.Event()
    lease = CountingLease(service)
    original = MemoryService.recall

    def held(self, *args, **kwargs):
        entered.set()
        release.wait(10)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "recall", held)

    async def scenario():
        server = build_server(lease=lease, workers=2)
        async with create_connected_server_and_client_session(server) as client:
            pending = asyncio.ensure_future(call(client, "memory_recall", task="x"))
            while not entered.is_set():
                await asyncio.sleep(0.02)
            pending.cancel()
            release.set()
            with contextlib.suppress(BaseException):
                await pending
            for _ in range(100):                           # the worker finishes on its own
                if lease.exited:
                    break
                await asyncio.sleep(0.05)

    run(scenario())
    release.set()
    assert (lease.entered, lease.exited) == (1, 1)


def test_a_provider_that_fails_closed_still_gives_an_error_with_workers(service):
    from agent_memory.service import ServiceError

    def lease():
        raise ServiceError("forbidden", "no authenticated caller")

    async def scenario():
        async with create_connected_server_and_client_session(
                build_server(lease=lease, workers=2)) as client:
            return await call(client, "memory_status")

    is_error, text = run(scenario())
    assert is_error and json.loads(text)["error"]["code"] == "forbidden"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_mcp_workers.py -q -p no:cacheprovider`
Expected: FAIL (`build_server() got an unexpected keyword argument 'lease'`, tools not coroutines).

- [ ] **Step 3: Implement**

In `src/agent_memory/mcp_server.py`:

1. Change the stdlib import block to add `contextlib` and `functools`:

```python
import contextlib
import functools
import json
import logging
import time
from typing import Any, Callable, ContextManager, Dict, List, Optional, Tuple
```

2. After the `try/except ImportError` block that imports `mcp`, add `from anyio import CapacityLimiter, to_thread` (the file already requires `mcp`, which requires anyio).

3. Replace `build_server`'s signature, docstring and the body up to and including the nested `run` function (from `def build_server(` through the end of `run`, i.e. just before the first `@app.tool(`) with:

```python
def build_server(service: Optional[MemoryService] = None, *,
                 provider: Optional[Callable[[], Tuple[Any, str]]] = None,
                 lease: Optional[Callable[[], ContextManager[Tuple[Any, str]]]] = None,
                 workers: Optional[int] = None,
                 trust_client_name: Optional[bool] = None,
                 observer: Optional[Callable[[str, str, int], None]] = None,
                 scrub: Optional[Callable[[str], str]] = None,
                 settings: Optional[Dict[str, Any]] = None) -> FastMCP:
    """`service` serves stdio: one service, read-write, the client's own name stamped on writes.
    A `provider` serves many callers: it returns (service, role) for the current call. A `lease`
    does the same but returns a context manager yielding (service, role), held for the whole
    call, so what it guards cannot be closed under a running call. Pass exactly one of the
    three. The client-reported name is trusted only when `trust_client_name` says so; left unset
    it is trusted for a plain `service` and ignored otherwise. `observer` hears every call's
    outcome and duration (from acceptance, so a queue wait counts) and `scrub` cleans every
    error message that leaves.

    `workers` None runs each call inline on the event loop (stdio: one client, so nothing waits
    on anything else). An int runs each call in a worker thread, at most that many at once; calls
    over the cap queue, and the event loop stays free for everything else."""
    if sum(item is not None for item in (service, provider, lease)) != 1:
        raise ValueError("pass exactly one of a service, a provider or a lease")
    if workers is not None and workers < 1:
        raise ValueError("workers must be at least 1")
    trust = (service is not None) if trust_client_name is None else trust_client_name
    if service is not None:
        lease = lambda: contextlib.nullcontext((service, "read-write"))   # noqa: E731
    elif provider is not None:
        lease = lambda: contextlib.nullcontext(provider())                # noqa: E731
    app = FastMCP("agent-memory", instructions=block("mcp"), **(settings or {}))

    def execute(ctx: Context, tool: str, started: float, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        outcome = "ok"
        try:
            with lease() as (target, role):
                if role != "read-write" and tool not in READ_ONLY_TOOLS:
                    raise ServiceError("forbidden", "this token is read-only: it can recall, "
                                                    "check and read the status, not change the memory")
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

    limiter: List[CapacityLimiter] = []        # built on first use: anyio needs a running loop

    async def dispatch(ctx: Context, tool: str, **kwargs: Any) -> Dict[str, Any]:
        started = time.monotonic()
        if workers is None:
            return execute(ctx, tool, started, kwargs)
        if not limiter:
            limiter.append(CapacityLimiter(workers))
        # The worker runs to completion even if the client goes away (anyio's default), so a
        # write is never left half done and its lease is always released.
        return await to_thread.run_sync(functools.partial(execute, ctx, tool, started, kwargs),
                                        limiter=limiter[0])
```

4. Make the eleven tool functions coroutines that await the dispatcher. Run exactly this (it changes only the `def` keyword of the eleven `memory_*` tool functions and the `run(` call in their bodies; it is safe on CRLF files):

```bash
sed -i -E 's/^    def (memory_[a-z_]+)\(/    async def \1(/; s/return run\(ctx/return await dispatch(ctx/' src/agent_memory/mcp_server.py
git diff --stat src/agent_memory/mcp_server.py
grep -c "async def memory_" src/agent_memory/mcp_server.py
grep -c "return await dispatch(ctx" src/agent_memory/mcp_server.py
```
Expected: both counts are `11`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_mcp_workers.py tests/test_mcp_roles.py tests/test_mcp_server.py tests/test_mcp_capture.py tests/test_mcp_concurrency.py -q -p no:cacheprovider`
Expected: all pass (stdio and provider tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/mcp_server.py tests/test_mcp_workers.py
git commit -m "feat: run HTTP tool calls in a bounded pool of worker threads"
```

---

### Task 3: Wire the HTTP app: leases, the cap, and shutdown without blocking the loop

**Files:**
- Modify: `src/agent_memory/remote/server.py` (`build_app`, `CloseOnShutdown`, imports, `DEFAULT_WORKERS`)
- Create: `tests/test_remote_workers.py`

**Interfaces:**
- Consumes: `Principals.lease`, `Principals.close(drain_seconds)`, `Principals.leases_held`, `.retired_count`, `.open_count` (Task 1); `build_server(lease=..., workers=...)` (Task 2); `current_principal`.
- Produces: `build_app(base, tokens, *, public_url="", limiter=None, max_workers=DEFAULT_WORKERS)` returning `(asgi_app, principals)` as before; module constant `DEFAULT_WORKERS = 8`. The lifespan-shutdown close runs in a worker thread so the loop stays free while it drains.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_remote_workers.py`:

```python
import concurrent.futures
import threading
import time

import pytest

pytest.importorskip("mcp")
from starlette.testclient import TestClient                          # noqa: E402

from agent_memory.remote.server import build_app                     # noqa: E402
from agent_memory.remote.tokens import TokenFile                     # noqa: E402
from agent_memory.service import MemoryService, resolve_config       # noqa: E402
from test_remote_app import rpc, tool                                # noqa: E402

ADDRESS = "http://127.0.0.1:8765"


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


class Gate:
    """Holds calls inside `MemoryService._context` (so inside the service lock and the lease)
    until released, and records how many are inside and which services they hold."""

    def __init__(self):
        self.release = threading.Event()
        self.services = []
        self._lock = threading.Lock()

    def enter(self, service):
        with self._lock:
            self.services.append(service)

    @property
    def entered(self):
        with self._lock:
            return len(self.services)

    def wait_entered(self, count, seconds=10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.entered >= count:
                return True
            time.sleep(0.02)
        return False


@pytest.fixture
def gate(monkeypatch):
    held = Gate()
    original = MemoryService._context

    def hold(self, context):
        held.enter(self)
        held.release.wait(15)
        return original(self, context)

    monkeypatch.setattr(MemoryService, "_context", hold)
    yield held
    held.release.set()


def recall(client, secret):
    return tool(client, secret, "memory_recall", task="anything")


def test_a_held_call_does_not_delay_the_guard_or_another_token(base, tokens, gate):
    a, b = tokens.create("alice", "read-write"), tokens.create("bob", "read-write")
    app, principals = build_app(base, tokens)
    with TestClient(app, base_url=ADDRESS) as client:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            held = pool.submit(recall, client, a)
            assert gate.wait_entered(1)
            started = time.monotonic()
            unauthenticated = rpc(client, None, "tools/list")
            assert unauthenticated.status_code == 401
            assert time.monotonic() - started < 0.5
            started = time.monotonic()
            failed, _ = tool(client, b, "memory_status")
            assert not failed and time.monotonic() - started < 0.5
            gate.release.set()
            assert not held.result(15)[0]
    principals.close()


def test_calls_by_one_token_run_one_after_the_other_and_by_two_tokens_together(base, tokens, monkeypatch):
    a, b = tokens.create("alice", "read-write"), tokens.create("bob", "read-write")
    running, peak, lock = [0], [0], threading.Lock()
    original = MemoryService._context

    def counted(self, context):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.3)
        try:
            return original(self, context)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(MemoryService, "_context", counted)
    app, principals = build_app(base, tokens)
    with TestClient(app, base_url=ADDRESS) as client:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda secret: recall(client, secret), [a, a]))
        assert peak[0] == 1                                 # the service lock serialises one token
        peak[0] = 0
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda secret: recall(client, secret), [a, b]))
        assert peak[0] == 2                                 # two tokens overlap
    principals.close()


def test_a_revoked_tokens_running_call_finishes_and_its_service_closes_after(base, tokens, gate):
    a, b = tokens.create("alice", "read-write"), tokens.create("bob", "read-write")
    app, principals = build_app(base, tokens)
    with TestClient(app, base_url=ADDRESS) as client:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            held = pool.submit(recall, client, a)
            assert gate.wait_entered(1)
            service = gate.services[0]
            tokens.revoke("alice")
            assert not tool(client, b, "memory_status")[0]  # the next call sweeps
            assert principals.retired_count == 1 and not service._closed
            gate.release.set()
            assert not held.result(15)[0]                   # the revoked token still got its result
        deadline = time.monotonic() + 5
        while principals.retired_count and time.monotonic() < deadline:
            time.sleep(0.02)
        assert service._closed and principals.retired_count == 0
        assert rpc(client, a, "tools/list").status_code == 401   # and its next request is refused
    principals.close()


def test_shutdown_waits_for_a_call_in_flight_without_blocking_the_loop(base, tokens, gate):
    a = tokens.create("alice", "read-write")
    app, principals = build_app(base, tokens)
    with TestClient(app, base_url=ADDRESS) as client:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            held = pool.submit(recall, client, a)
            assert gate.wait_entered(1)
            closer = threading.Thread(target=principals.close, kwargs={"drain_seconds": 10})
            closer.start()
            closer.join(0.4)
            assert closer.is_alive()                        # draining, not stuck
            assert rpc(client, None, "tools/list").status_code == 401   # the loop still answers
            gate.release.set()
            assert not held.result(15)[0]                   # the call finished normally
            closer.join(10)
            assert not closer.is_alive() and principals.open_count == 0
            assert gate.services[0]._closed


def test_calls_over_the_cap_queue_and_the_guard_still_answers(base, tokens, gate):
    secrets = [tokens.create(f"agent{i}", "read-write") for i in range(3)]
    app, principals = build_app(base, tokens, max_workers=2)
    with TestClient(app, base_url=ADDRESS) as client:
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            futures = [pool.submit(recall, client, secret) for secret in secrets]
            assert gate.wait_entered(2)
            time.sleep(0.4)
            assert gate.entered == 2                        # the third waits for a worker
            started = time.monotonic()
            assert rpc(client, None, "tools/list").status_code == 401
            assert time.monotonic() - started < 0.5
            gate.release.set()
            assert all(not f.result(20)[0] for f in futures)
        assert gate.entered == 3
    principals.close()


def test_every_lease_is_released_after_ok_error_and_forbidden_calls(base, tokens):
    rw, ro = tokens.create("alice", "read-write"), tokens.create("bob", "read-only")
    app, principals = build_app(base, tokens)
    with TestClient(app, base_url=ADDRESS) as client:
        assert not tool(client, rw, "memory_status")[0]
        assert tool(client, rw, "memory_retract", id="no-such-note")[0]      # not_found
        assert tool(client, ro, "memory_remember", kind="quirk",
                    fields={"description": "d"})[0]                           # forbidden
        assert principals.leases_held == 0
    principals.close()


def test_a_note_written_from_a_worker_thread_carries_the_token_name(base, tokens, monkeypatch):
    secret = tokens.create("alice", "read-write")
    threads = []
    original = MemoryService.remember

    def spy(self, *args, **kwargs):
        threads.append(threading.get_ident())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "remember", spy)
    app, principals = build_app(base, tokens)
    with TestClient(app, base_url=ADDRESS) as client:
        failed, _made = tool(client, secret, "memory_remember", kind="quirk",
                             fields={"description": "worker thread note"})
        assert not failed
        failed, status = tool(client, secret, "memory_status")
        assert not failed and status["agent"] == "alice"     # the token's name, not a client claim
    assert threads and threads[0] != threading.get_ident()   # it ran on a worker, not the test's thread
    principals.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_remote_workers.py -q -p no:cacheprovider`
Expected: FAIL (`build_app() got an unexpected keyword argument 'max_workers'`, and the guard test waits for the held call).

- [ ] **Step 3: Implement**

In `src/agent_memory/remote/server.py`:

1. Add `from anyio import to_thread` with the other third-party imports (after `from mcp.server.transport_security import TransportSecuritySettings`), and add `DEFAULT_WORKERS = 8` beside `LOOPBACK_HOSTS`.

2. In `CloseOnShutdown.__call__`, replace the body of `watching` so the close runs in a worker thread (the loop stays free to deliver in-flight responses while a drain waits):

```python
        async def watching(message):
            if message["type"] in ("lifespan.shutdown.complete", "lifespan.shutdown.failed"):
                try:
                    await to_thread.run_sync(self.principals.close)   # idempotent; drains running calls
                except Exception:
                    self.principals.close()
            await send(message)
```

3. Replace `build_app`'s signature, the `provider` function and the `build_server(...)` call:

```python
def build_app(base: ServiceConfig, tokens: TokenFile, *, public_url: str = "",
              limiter: Optional[FailureLimiter] = None, max_workers: int = DEFAULT_WORKERS):
    """(asgi_app, principals). The app authenticates every request before anything else. Tool
    calls run in at most `max_workers` worker threads; the guard stays on the event loop."""
    principals = Principals(base, tokens)
    hosts = LOOPBACK_HOSTS + public_hosts(public_url)

    def lease():
        principal = current_principal.get()
        if principal is None:                          # the guard always sets it: fail closed
            raise ServiceError("forbidden", "no authenticated caller")
        return principals.lease(principal)
```

   keep the `observer` function as it is, and change the `build_server(` call's first arguments from `provider=provider, trust_client_name=False,` to `lease=lease, workers=max_workers, trust_client_name=False,` (the rest of the call is unchanged).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_remote_workers.py tests/test_remote_app.py tests/test_remote_guard.py tests/test_remote_leases.py tests/test_mcp_workers.py -q -p no:cacheprovider`
Expected: all pass. If a test in `test_remote_workers.py` fails only on a timing bound (0.5 s), re-run it alone before touching code. If `TestClient` proves unsafe from several threads in your environment, say so in the report and use `httpx` against a real `uvicorn` server as `tests/test_remote_e2e.py` does.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/remote/server.py tests/test_remote_workers.py
git commit -m "feat: serve HTTP tool calls from the worker pool with leased services"
```

---

### Task 4: Stress run, a real-server check, and the latency measurement

**Files:**
- Modify: `tests/test_remote_workers.py` (append the stress test)
- Modify: `tests/test_remote_e2e.py` (append one test)
- Create: `tests/remote_latency.py` (a measurement script, not a test: no `test_` prefix)

**Interfaces:**
- Consumes: `build_app(..., max_workers=)`, `Principals.leases_held`, `.retired_count` (Tasks 1, 3); the `live` fixture and `call`, `run` helpers in `tests/test_remote_e2e.py`.
- Produces: nothing later tasks use except the measured figures for the docs.

- [ ] **Step 1: Write the stress test**

Append to `tests/test_remote_workers.py`:

```python
def test_a_mixed_burst_with_a_revoke_in_the_middle_loses_nothing(base, tokens):
    names = [f"agent{i}" for i in range(8)]
    secrets = {name: tokens.create(name, "read-write") for name in names}
    app, principals = build_app(base, tokens, max_workers=4)
    lock = threading.Lock()
    outcomes = []                                           # (name, status, text)
    counter = [0]

    def attempt(client, name, secret, i):
        kind = i % 4
        if kind == 0:
            name_, args = "memory_recall", {"task": f"queue quirk {i}"}
        elif kind == 1:
            name_, args = "memory_remember", {"kind": "quirk",
                                              "fields": {"description": f"burst note {i}"}}
        elif kind == 2:
            name_, args = "memory_status", {}
        else:
            name_, args = "memory_check", {"approach": f"approach {i}"}
        response = rpc(client, secret, "tools/call", {"name": name_, "arguments": args}, call_id=i)
        text = response.text if response.status_code != 200 else \
            response.json()["result"]["content"][0]["text"]
        with lock:
            outcomes.append((name, response.status_code, text))

    def work(client, i):
        with lock:
            counter[0] += 1
            first_half_done = counter[0] == 80
        if first_half_done:                                 # in the middle: one token goes, one arrives
            tokens.revoke("agent3")
            secrets["agent9"] = tokens.create("agent9", "read-write")
        name = names[i % 8]
        attempt(client, name, secrets[name], i)

    with TestClient(app, base_url=ADDRESS) as client:
        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            for future in [pool.submit(work, client, i) for i in range(200)]:
                future.result(120)
        assert not tool(client, secrets["agent9"], "memory_status")[0]
    assert len(outcomes) == 200
    for name, status, text in outcomes:
        assert status == 200 or (status in (401, 429) and name == "agent3"), (name, status, text)   # 429: the failure limiter
        assert "service is closed" not in text and '"internal"' not in text, text
    assert principals.leases_held == 0 and principals.retired_count == 0
    principals.close()
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/test_remote_workers.py -q -p no:cacheprovider`
Expected: PASS. A failure showing `service is closed` is a real bug in Task 1 or 3: stop and report, do not loosen the assertion.

- [ ] **Step 3: Add the real-server check**

Append to `tests/test_remote_e2e.py` (the module already imports `threading`, `time`, `urllib.error`, `urllib.request`, `ClientSession`, `streamablehttp_client` and `MemoryService`, and defines `live`, `call` and `run`):

```python
def test_a_held_call_does_not_stop_other_callers_on_a_real_server(live, monkeypatch):
    url, tokens, _ = live
    a, b = tokens.create("alice", "read-write"), tokens.create("bob", "read-write")
    entered, release = threading.Event(), threading.Event()
    original = MemoryService._context

    def held(self, context):
        entered.set()
        release.wait(20)
        return original(self, context)

    monkeypatch.setattr(MemoryService, "_context", held)
    result = {}

    def hold_one():
        async def go():
            async with streamablehttp_client(url, headers={"Authorization": f"Bearer {a}"}) as (r, w, _):
                async with ClientSession(r, w) as client:
                    await client.initialize()
                    return await call(client, "memory_recall", task="anything")
        result["alice"] = run(go())

    async def bob_status():
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {b}"}) as (r, w, _):
            async with ClientSession(r, w) as client:
                await client.initialize()
                return await call(client, "memory_status")

    holder = threading.Thread(target=hold_one)
    holder.start()
    try:
        assert entered.wait(20)
        started = time.monotonic()
        request = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=5)
        assert refused.value.code == 401 and time.monotonic() - started < 1.0
        started = time.monotonic()
        failed, _text = run(bob_status())
        assert not failed and time.monotonic() - started < 3.0
    finally:
        release.set()
        holder.join(30)
    assert not result["alice"][0]
```

Run: `python -m pytest tests/test_remote_e2e.py -q -p no:cacheprovider`
Expected: PASS, including the existing graceful-stop test.

- [ ] **Step 4: Add the latency script and measure**

Create `tests/remote_latency.py`:

```python
"""Median memory_recall latency through the in-process HTTP app. Not a test (no `test_` prefix).

    PYTHONPATH=<checkout>/src python tests/remote_latency.py

Run it against `main` (a worktree) and against this branch in the same session and compare.
"""
import statistics
import sys
import tempfile
import time
from pathlib import Path

from starlette.testclient import TestClient

from agent_memory.remote.server import build_app
from agent_memory.remote.tokens import TokenFile
from agent_memory.service import resolve_config

ACCEPT = "application/json, text/event-stream"
TOPICS = ["cache", "queue", "retry", "auth", "migration", "schema", "logging", "deploy"]


def main(calls: int = 200) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        (root / ".git").mkdir(parents=True)
        (root / "src").mkdir()
        (root / "src" / "cache.py").write_text("x = 1\n")
        base = resolve_config(repo=str(root), agent="remote", env={})
        tokens = TokenFile(Path(tmp) / "tokens.json")
        secret = tokens.create("bench", "read-write")
        app, principals = build_app(base, tokens)
        headers = {"Accept": ACCEPT, "Authorization": f"Bearer {secret}"}
        counter = [0]

        def tool(name, **arguments):
            counter[0] += 1
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": counter[0],
                                                 "method": "tools/call",
                                                 "params": {"name": name, "arguments": arguments}},
                                   headers=headers)
            response.raise_for_status()

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            for i in range(40):
                topic = TOPICS[i % len(TOPICS)]
                tool("memory_remember", kind="quirk",
                     fields={"description": f"the {topic} layer misbehaves under load case {i}"})
            for i in range(20):
                tool("memory_recall", task=f"fix the {TOPICS[i % len(TOPICS)]} problem")
            times = []
            for i in range(calls):
                started = time.perf_counter()
                tool("memory_recall", task=f"fix the {TOPICS[i % len(TOPICS)]} problem")
                times.append((time.perf_counter() - started) * 1000)
        principals.close()
    times.sort()
    print(f"median {statistics.median(times):.2f} ms  p95 {times[int(0.95 * len(times)) - 1]:.2f} ms "
          f"over {calls} calls")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200)
```

Measure `main` and this branch alternately (three runs each) in one session, from the repo root:

```bash
SCRATCH="C:/Users/MAHES_~1/AppData/Local/Temp/claude/9ae08f545ccd6de3/fd97cf6e-7520-41c6-abee-ad0ca073c933/scratchpad"
git worktree add "$SCRATCH/main-wt" main
for i in 1 2 3; do
  echo "main:   $(PYTHONPATH=$SCRATCH/main-wt/src python tests/remote_latency.py)"
  echo "branch: $(PYTHONPATH=src python tests/remote_latency.py)"
done
git worktree remove --force "$SCRATCH/main-wt"
```
Record the six lines. The acceptance bound (spec section 1): the branch's median is within 25% or 2 ms (whichever is larger) of main's median in the same session. If it is outside, report the numbers and stop; do not tune.

- [ ] **Step 5: Commit**

```bash
git add tests/test_remote_workers.py tests/test_remote_e2e.py tests/remote_latency.py
git commit -m "test: stress, real-server and latency checks for the worker pool"
```

---

### Task 5: Docs, measured figures, and the full suite

**Files:**
- Modify: `README.md` (the remote server "Known limits" list, around the line "Calls are served one at a time")
- Modify: `docs/superpowers/specs/2026-11-20-remote-mcp-design.md` (the Concurrency paragraph and the known-limits line in section 5/6)
- Modify: `docs/superpowers/specs/2026-11-22-remote-worker-threads-design.md` (a "Status" line and a "Measured" section)

**Interfaces:**
- Consumes: the latency figures from Task 4; the final test count from the full run.
- Produces: docs only.

- [ ] **Step 1: README**

Replace the two lines starting `- Calls are served one at a time; a slow call (for example a store-lock wait of up to 10 s while` with:

```
- Tool calls run in a pool of 8 worker threads, so a slow call (for example a store-lock wait of up to
  10 s while another process checkpoints) no longer delays authentication or other tokens. Calls over
  the 8 queue without a timeout, calls by one token still run one after the other, and a client that
  disconnects does not stop its call. A revoked token's running call finishes; its service closes
  afterwards. A normal stop waits up to 15 s for running calls.
```
(Keep the exact surrounding list formatting and the file's CRLF line endings; check with `git diff --stat README.md`.)

- [ ] **Step 2: The remote spec**

In `docs/superpowers/specs/2026-11-20-remote-mcp-design.md`, at the end of the **Concurrency.** paragraph add: ` **Superseded by `2026-11-22-remote-worker-threads-design.md`:** tool calls now run in a bounded pool of worker threads with leased services; the text above describes the first implementation.` and change the known-limits line "Tool calls are served one at a time (see Concurrency) ..." to point at the new document's section 6 instead.

- [ ] **Step 3: The new spec**

In `docs/superpowers/specs/2026-11-22-remote-worker-threads-design.md` change the Status line to `Status: implemented (branch feature/remote-worker-threads); approved in conversation ...` (keep the rest of the line) and append:

```
## 8. Measured

Median `memory_recall` over 200 calls through the in-process HTTP app (`tests/remote_latency.py`), main and
this branch alternated in one session, three runs each:

| Run | main | branch |
|---|---|---|
| 1 | <median> ms | <median> ms |
| 2 | <median> ms | <median> ms |
| 3 | <median> ms | <median> ms |

<One sentence: the medians of medians and whether they are inside the bound of section 1.>
With one call held, an unauthenticated request was answered in <n> ms (before this change: 2.75 s with the
call held for 3 s).
```
Fill every `<...>` with the figures from Task 4 and from the real-server test's timing (add a one-off print if you need the number; do not commit the print). No placeholder may remain.

- [ ] **Step 4: Full suite**

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: all pass. Then `PYTHONPATH=src python -m tests.eval.run --service` still reports recall 1.00 (30/30), leaks 0, abstention 1.00. Note the pass count in the report.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-11-20-remote-mcp-design.md docs/superpowers/specs/2026-11-22-remote-worker-threads-design.md
git commit -m "docs: worker-thread model, limits and measured latency"
```

Do not push. The final report lists the commits on the branch and the measured figures.
