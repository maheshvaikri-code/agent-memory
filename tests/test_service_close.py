"""Closing after a successful call must never turn it into a failure: every write is already
durable in the journal, so a checkpoint that cannot get the lock in time is skipped. Before, a
busy lock at close() made `amem session start` exit 2 after it had created the session (a retry
made a second one) and `amem feedback` count twice on retry."""
import json
import subprocess
import sys

import pytest
from click.testing import CliRunner

from agent_memory import AgentMemory
from agent_memory.cli import main
from agent_memory.service import MemoryService, resolve_config
from agent_memory.storage.backend import JournalBackend
from storage_helpers import child_env

HOLD = """
import sys
from agent_memory.storage.lock import FileLock
lock = FileLock(sys.argv[1], timeout=30)
lock.acquire()
print("locked", flush=True)
sys.stdin.read()          # hold it until the test closes our stdin
lock.release()
"""


class Holder:
    """Another process holding the store's lock (an OS lock: per process on POSIX)."""

    def __init__(self, store):
        self.proc = subprocess.Popen([sys.executable, "-c", HOLD, store + ".lock"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                     env=child_env())
        assert self.proc.stdout.readline().strip() == "locked"

    def release(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()


def split_runner():
    """stdout and stderr apart (click 8.1 needs mix_stderr=False; 8.2 always separates them)."""
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def short_locks(monkeypatch):
    real = JournalBackend.__init__

    def init(self, path, **options):
        options["lock_timeout"] = 0.3
        real(self, path, **options)
    monkeypatch.setattr(JournalBackend, "__init__", init)


def test_close_with_the_lock_held_elsewhere_does_not_raise(repo, short_locks):
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    service.remember("convention", {"category": "naming", "rule": "keys use colons"})
    store = service.config.store_path
    holder = Holder(store)
    try:
        service.close()                       # checkpoint skipped: the write is in the journal
    finally:
        holder.release()
    recalled = AgentMemory.load(store).recall(task="keys use colons").items
    assert any("colons" in i.text for i in recalled)


def test_a_command_whose_close_hits_a_busy_lock_still_reports_its_result(repo, short_locks,
                                                                         monkeypatch):
    held = []
    real = MemoryService.remember

    def remember_then_lose_the_lock(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        held.append(Holder(self.config.store_path))     # another process takes the lock now
        return result
    monkeypatch.setattr(MemoryService, "remember", remember_then_lose_the_lock)
    try:
        out = CliRunner().invoke(main, ["remember", "convention", "-F", "category=naming",
                                        "-F", "rule=keys use colons", "--repo", str(repo),
                                        "--json"])
    finally:
        for holder in held:
            holder.release()
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout)["created"] is True


def test_a_close_failure_never_replaces_a_successful_result(repo, monkeypatch):
    from agent_memory.service import ServiceError

    def broken_close(self):
        raise ServiceError("store_busy", "another process holds the store; retry shortly")
    monkeypatch.setattr(MemoryService, "close", broken_close)
    runner = split_runner()
    out = runner.invoke(main, ["session", "start", "fix the webhook", "--repo", str(repo),
                               "--json"])
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout)["session_id"]
    assert "Traceback" not in out.output and out.stderr.count("\n") == 1
    assert "already saved" in out.stderr


def test_an_action_error_is_not_hidden_by_a_close_error(repo, monkeypatch):
    from agent_memory.service import ServiceError

    def broken_close(self):
        raise ServiceError("store_busy", "another process holds the store; retry shortly")
    monkeypatch.setattr(MemoryService, "close", broken_close)
    out = CliRunner().invoke(main, ["retract", "no_such_id", "--repo", str(repo), "--json"])
    assert out.exit_code == 2 and json.loads(out.stdout)["error"]["code"] == "not_found"
