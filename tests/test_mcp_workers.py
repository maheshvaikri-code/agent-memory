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


def error_code(text):
    """FastMCP prefixes a tool error with 'Error executing tool <name>: '; the JSON follows."""
    return json.loads(text[text.index("{"):])["error"]["code"]


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
    assert is_error and error_code(text) == "not_found"
    assert (lease.entered, lease.exited) == (1, 1)


def test_the_lease_is_released_when_the_role_is_refused(service):
    lease = CountingLease(service, role="read-only")

    async def scenario():
        async with create_connected_server_and_client_session(
                build_server(lease=lease, workers=2)) as client:
            return await call(client, "memory_remember", kind="quirk", fields={"description": "d"})

    is_error, text = run(scenario())
    assert is_error and error_code(text) == "forbidden"
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
    assert is_error and error_code(text) == "forbidden"
