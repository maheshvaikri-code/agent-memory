import logging
import threading
import time

import pytest

pytest.importorskip("mcp")

from agent_memory.remote import server as server_module              # noqa: E402
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


def slow_factory(monkeypatch, slow_agents, gate, entered=None):
    """Replace the service constructor: it waits on `gate` for the named agents and otherwise
    builds the real service. Returns the list of every service it built."""
    real, built = server_module.MemoryService, []

    def factory(config):
        if config.agent in slow_agents:
            if entered is not None:
                entered.set()
            gate.wait(10)
        service = real(config)
        built.append(service)
        return service

    monkeypatch.setattr(server_module, "MemoryService", factory)
    return built


def test_a_slow_first_open_does_not_hold_up_another_principal(base, tokens, monkeypatch):
    principals = Principals(base, tokens)
    alice, bob = principal(tokens, "alice"), principal(tokens, "bob")
    gate, entered, done = threading.Event(), threading.Event(), threading.Event()
    slow_factory(monkeypatch, {"alice"}, gate, entered)
    with principals.lease(bob):                           # bob is already open
        pass

    def first_call():
        with principals.lease(alice):
            done.set()

    worker = threading.Thread(target=first_call)
    worker.start()
    try:
        assert entered.wait(5)                            # alice's service is being built
        started = time.monotonic()
        with principals.lease(bob):
            pass
        assert time.monotonic() - started < 1.0           # not queued behind alice's open
        assert not done.is_set()
    finally:
        gate.set()
        worker.join(10)
    assert done.is_set() and principals.open_count == 2 and principals.leases_held == 0
    principals.close()


def test_close_during_a_first_open_closes_the_new_service_and_refuses_the_lease(base, tokens,
                                                                                  monkeypatch):
    principals = Principals(base, tokens)
    alice = principal(tokens, "alice")
    gate, entered, outcome = threading.Event(), threading.Event(), {}
    built = slow_factory(monkeypatch, {"alice"}, gate, entered)

    def first_call():
        try:
            with principals.lease(alice):
                outcome["leased"] = True
        except ServiceError as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=first_call)
    worker.start()
    try:
        assert entered.wait(5)
        principals.close(drain_seconds=0.2)               # nothing is leased yet
    finally:
        gate.set()
        worker.join(10)
    assert "leased" not in outcome and outcome["error"].code == "internal"
    assert len(built) == 1 and built[0]._closed           # nothing leaks
    assert principals.open_count == 0 and principals.leases_held == 0


def test_two_racing_first_leases_share_one_service_and_the_loser_is_closed(base, tokens,
                                                                            monkeypatch):
    principals = Principals(base, tokens)
    alice = principal(tokens, "alice")
    both_building = threading.Barrier(2, timeout=10)
    real, built, seen = server_module.MemoryService, [], []

    def factory(config):
        both_building.wait()                              # each is mid-open when the other arrives
        service = real(config)
        built.append(service)
        return service

    monkeypatch.setattr(server_module, "MemoryService", factory)

    def first_call():
        with principals.lease(alice) as (service, _):
            seen.append(service)

    workers = [threading.Thread(target=first_call) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
    assert len(seen) == 2 and seen[0] is seen[1]
    assert len(built) == 2 and principals.open_count == 1
    kept = [s for s in built if s is seen[0]]
    lost = [s for s in built if s is not seen[0]]
    assert len(kept) == 1 and len(lost) == 1
    assert lost[0]._closed and not kept[0]._closed
    principals.close()


def test_the_lifespan_close_runs_even_when_the_lifespan_task_is_cancelled():
    import anyio
    from agent_memory.remote.server import CloseOnShutdown

    closed = []

    class Spy:
        def close(self):
            closed.append(True)

    async def inner(scope, receive, send):
        await send({"type": "lifespan.shutdown.complete"})

    sent = []

    async def send(message):
        sent.append(message["type"])

    async def scenario():
        with anyio.CancelScope() as scope:
            scope.cancel()                                # the shutdown arrives already cancelled
            await CloseOnShutdown(inner, Spy())({"type": "lifespan"}, None, send)

    anyio.run(scenario)
    assert closed == [True]
