"""`amem status`, `recall` and `guard` read the store without creating or checkpointing it.

The write commands and the MCP server keep opening it (tests elsewhere); these three go through
a read-only MemoryService: a missing store stays missing, an existing one is read (snapshot plus
journal replayed) and left byte-for-byte as it was."""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from agent_memory import AgentMemory
from agent_memory.cli import main
from agent_memory.service import MemoryService, ServiceError, resolve_config

CONVENTION = {"category": "naming", "rule": "cache keys use colon namespaces"}
BUG = {"name": "retry_storm", "signature": "unbounded retry on an external webhook"}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


def listing(root):
    """Every path under `root` (directories included), relative."""
    out = set()
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            out.add(os.path.relpath(os.path.join(base, name), root))
    return out


def file_bytes(*paths):
    return {str(p): (open(p, "rb").read() if os.path.exists(p) else None) for p in paths}


def cli(repo, *args, json_out=True, expect=0):
    argv = [*args, "--repo", str(repo)] + (["--json"] if json_out else [])
    result = CliRunner().invoke(main, argv)
    assert result.exit_code == expect, result.output
    return json.loads(result.output) if json_out else result.output


# -- a store that does not exist -------------------------------------------------------

def test_read_commands_on_a_fresh_repo_create_nothing(repo):
    before = listing(repo)
    recalled = cli(repo, "recall", "add retry logic to the webhook", "-f", "src/cache.py")
    assert recalled == {"items": [], "prompt": "", "tokens": 0,
                        "note": "nothing relevant is recorded for this task"}
    guarded = cli(repo, "guard", "retry unbounded on the webhook")
    assert guarded == {"items": [], "prompt": "", "warn": False,
                       "note": "no recorded guardrail matches this approach"}
    status = cli(repo, "status")
    assert status["store_exists"] is False
    assert status["store"] == str(repo / ".agent-memory" / "project.memory")
    assert status["counts"] == {} and status["total_active"] == 0
    assert status["unfolded_transactions"] == 0 and status["warnings"] == []
    assert status["shared"] is False and status["last_write"] == ""
    assert listing(repo) == before
    assert not (repo / ".agent-memory").exists()


def test_human_status_says_no_store_exists_yet(repo):
    text = cli(repo, "status", json_out=False)
    assert "no store yet" in text
    assert not (repo / ".agent-memory").exists()


def test_human_recall_and_guard_on_a_fresh_repo(repo):
    assert "nothing relevant is recorded" in cli(repo, "recall", "anything", json_out=False)
    assert "no recorded guardrail" in cli(repo, "guard", "anything", json_out=False)
    assert not (repo / ".agent-memory").exists()


def test_an_explicit_missing_store_path_is_not_created(repo, tmp_path):
    store = tmp_path / "elsewhere" / "team.memory"
    for args in (("recall", "x"), ("guard", "x"), ("status",)):
        cli(repo, *args, "--store", str(store))
    assert not (tmp_path / "elsewhere").exists()


def test_a_write_command_still_creates_the_store(repo):
    cli(repo, "remember", "convention", "-F", "category=naming", "-F", "rule=colon keys")
    assert (repo / ".agent-memory" / "project.memory").exists()
    assert cli(repo, "status")["store_exists"] is True


def test_an_orphaned_journal_is_still_reported_corrupt(repo):
    store = repo / ".agent-memory" / "project.memory"
    store.parent.mkdir()
    writer = AgentMemory.open(str(store), repo_root=str(repo))
    writer.remember("convention", CONVENTION)
    os.remove(store)                      # the journal holds acknowledged writes
    out = cli(repo, "status", expect=2)
    assert out["error"]["code"] == "store_corrupt"
    assert not store.exists()


# -- an existing store ---------------------------------------------------------------

def unfolded_store(repo):
    """A journaled store whose last writes are not folded into the snapshot (the writer has
    not closed: it is a live server). Returns the writer and the store path."""
    store = repo / ".agent-memory" / "project.memory"
    store.parent.mkdir()
    writer = AgentMemory.open(str(store), repo_root=str(repo), agent="server")
    writer.remember("convention", CONVENTION)
    writer.remember("bug_pattern", BUG)
    assert writer.unfolded_transactions() >= 2
    return writer, store


def test_reads_leave_an_unfolded_journal_byte_identical(repo):
    writer, store = unfolded_store(repo)
    try:
        files = [store, str(store) + ".journal", str(store) + ".lock"]
        before, names = file_bytes(*files), listing(repo)
        recalled = cli(repo, "recall", "how should I name cache keys")
        assert any("colon namespaces" in i["text"] for i in recalled["items"])
        guarded = cli(repo, "guard", "retry unbounded on the external webhook")
        assert guarded["warn"] is True
        status = cli(repo, "status")
        assert status["store_exists"] is True and status["shared"] is True
        assert status["counts"] == {"convention": 1, "bug_pattern": 1}
        assert status["unfolded_transactions"] == writer.unfolded_transactions() >= 2
        cli(repo, "status", json_out=False)
        assert file_bytes(*files) == before
        assert listing(repo) == names
    finally:
        writer.close()


def test_a_plain_store_stays_plain_and_byte_identical(repo, tmp_path):
    path = tmp_path / "plain.memory"
    plain = AgentMemory("plain", repo_root=str(repo))
    plain.remember("convention", CONVENTION)
    plain.save(str(path))
    before, names = path.read_bytes(), listing(tmp_path)
    recalled = cli(repo, "recall", "how should I name cache keys", "--store", str(path))
    assert recalled["items"]
    cli(repo, "guard", "rename the cache keys", "--store", str(path))
    status = cli(repo, "status", "--store", str(path))
    assert status["store_exists"] is True and status["shared"] is False
    assert status["total_active"] == 1
    assert path.read_bytes() == before
    assert listing(tmp_path) == names                  # no .journal, no .lock
    assert not os.path.exists(str(path) + ".journal")


def test_a_live_writers_later_write_is_visible_to_the_next_read(repo):
    writer = MemoryService(resolve_config(repo=str(repo), agent="server", env={}))
    try:
        writer.remember("convention", CONVENTION)
        assert cli(repo, "recall", "retry backoff for the webhook")["items"] == []
        writer.remember("bug_pattern", BUG)              # acknowledged after the first read
        recalled = cli(repo, "recall", "retry backoff for the webhook")
        bugs = [i for i in recalled["items"] if i["kind"] == "bug_pattern"]
        assert bugs and bugs[0]["agent"] == "server"
        assert cli(repo, "guard", "retry unbounded on the external webhook")["warn"] is True
        # the reader never took the write path: the writer can still write right away
        writer.remember("decision", {"title": "use exponential backoff",
                                     "context": "the webhook retried forever"})
        assert cli(repo, "status")["total_active"] == 3
    finally:
        writer.close()


SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def amem_process(repo, *args):
    """A separate `amem` process, as an agent runs it beside a live MCP server."""
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_MEMORY_")}
    env["PYTHONPATH"] = os.pathsep.join([SRC, env.get("PYTHONPATH", "")])
    done = subprocess.run([sys.executable, "-m", "agent_memory.cli", *args, "--repo", str(repo),
                           "--json"], stdin=subprocess.DEVNULL, capture_output=True, env=env,
                          timeout=120)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return json.loads(done.stdout)


def test_a_reader_process_beside_a_live_writer(repo):
    writer = MemoryService(resolve_config(repo=str(repo), agent="server", env={}))
    store = repo / ".agent-memory" / "project.memory"
    try:
        writer.remember("convention", CONVENTION)
        files = [store, str(store) + ".journal", str(store) + ".lock"]
        before, names = file_bytes(*files), listing(repo)
        assert amem_process(repo, "recall", "retry backoff for the webhook")["items"] == []
        assert file_bytes(*files) == before and listing(repo) == names
        writer.remember("bug_pattern", BUG)
        after_write = file_bytes(*files)
        recalled = amem_process(repo, "recall", "retry backoff for the webhook")
        assert any(i["kind"] == "bug_pattern" and i["agent"] == "server"
                   for i in recalled["items"])
        status = amem_process(repo, "status")
        assert status["unfolded_transactions"] == writer.status()["unfolded_transactions"] >= 2
        assert file_bytes(*files) == after_write and listing(repo) == names
        writer.remember("decision", {"title": "use exponential backoff",
                                     "context": "the webhook retried forever"})
    finally:
        writer.close()


def test_recall_with_a_session_still_works_read_only(repo):
    started = cli(repo, "session", "start", "fix the webhook")
    sid = started["session_id"]
    cli(repo, "remember", "open_question", "-F", "question=cap the retries?", "--session", sid)
    store = repo / ".agent-memory" / "project.memory"
    files = [store, str(store) + ".journal"]
    before = file_bytes(*files)
    out = cli(repo, "recall", "webhook retries", "--session", sid)
    assert any("cap the retries" in i["text"] for i in out["items"])
    assert file_bytes(*files) == before
    cli(repo, "recall", "x", "--session", "no-such-session", expect=2)


# -- the read-only service itself -------------------------------------------------------

def read_only_service(repo, **kw):
    return MemoryService(resolve_config(repo=str(repo), agent="reader", env={},
                                        read_only=True, **kw))


def test_a_read_only_service_refuses_every_write(repo):
    seeded = MemoryService(resolve_config(repo=str(repo), agent="seed", env={}))
    rec = seeded.remember("convention", CONVENTION)
    seeded.close()
    store = repo / ".agent-memory" / "project.memory"
    files = [store, str(store) + ".journal", str(store) + ".lock"]
    before = file_bytes(*files)
    svc = read_only_service(repo)
    writes = {
        "remember": lambda: svc.remember("convention", {"category": "x", "rule": "y"}),
        "feedback": lambda: svc.feedback(rec["id"], True),
        "supersede": lambda: svc.supersede(rec["id"], "convention", CONVENTION),
        "retract": lambda: svc.retract(rec["id"], "wrong"),
        "verify": lambda: svc.verify(rec["id"]),
        "session_start": lambda: svc.session_start("a task"),
        "session_end": lambda: svc.session_end("done"),
    }
    for name, call in writes.items():
        with pytest.raises(ServiceError) as exc:
            call()
        assert exc.value.code == "invalid_argument", name
        assert "read-only" in exc.value.message, name
    assert svc.recall("how should I name cache keys")["items"]
    assert svc.status()["total_active"] == 1
    svc.close()
    svc.close()
    assert file_bytes(*files) == before


def test_a_read_only_service_on_a_missing_store(repo):
    svc = read_only_service(repo)
    assert svc.status()["store_exists"] is False
    assert svc.recall("anything")["note"] == "nothing relevant is recorded for this task"
    assert svc.check("anything")["warn"] is False
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", CONVENTION)
    assert exc.value.code == "invalid_argument"
    svc.close()
    assert not (repo / ".agent-memory").exists()


def test_a_read_only_service_refuses_an_isonl_store_like_the_writer(repo, tmp_path):
    for read_only in (False, True):
        with pytest.raises(ServiceError) as exc:
            MemoryService(resolve_config(repo=str(repo), store=str(tmp_path / "x.isonl"),
                                         env={}, read_only=read_only))
        assert exc.value.code == "invalid_argument"


def test_the_writable_service_is_unchanged_by_default(repo):
    config = resolve_config(repo=str(repo), env={})
    assert config.read_only is False
    svc = MemoryService(config)
    assert svc.status()["store_exists"] is True and svc.status()["shared"] is True
    svc.close()
    assert (repo / ".agent-memory" / "project.memory").exists()
