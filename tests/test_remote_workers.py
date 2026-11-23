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
