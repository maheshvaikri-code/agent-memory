import json

import pytest
from click.testing import CliRunner

from agent_memory import AgentMemory
from agent_memory.cli import main


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def run(repo):
    runner = CliRunner()

    def invoke(*args, json_out=True, expect=0):
        argv = [*args, "--repo", str(repo)] + (["--json"] if json_out else [])
        result = runner.invoke(main, argv)
        assert result.exit_code == expect, result.output
        return json.loads(result.output) if json_out else result.output

    return invoke


def test_remember_then_recall_then_guard(run):
    out = run("remember", "convention", "-F", "category=naming",
              "-F", "rule=cache keys use colon namespaces", "-f", "src/cache.py")
    assert out["created"] is True and set(out) >= {"id", "kind", "created", "merged", "similar"}
    recalled = run("recall", "how should I name cache keys", "-f", "src/cache.py")
    assert set(recalled) == {"items", "prompt", "tokens", "note"} and recalled["items"]
    run("remember", "bug_pattern", "-F", "name=retry_storm",
        "-F", "signature=unbounded retry on an external webhook")
    guard = run("guard", "retry unbounded on the webhook")
    assert set(guard) == {"items", "prompt", "warn", "note"} and guard["warn"] is True


def test_plain_output_is_readable(run):
    run("remember", "convention", "-F", "category=naming", "-F", "rule=cache keys use colon namespaces")
    text = run("recall", "name cache keys", json_out=False)
    assert "colon namespaces" in text


def test_feedback_retract_verify_status(run):
    rec = run("remember", "convention", "-F", "category=naming", "-F", "rule=cache keys use colon")
    assert run("feedback", rec["id"], "--helpful")["ok"] is True
    assert run("feedback", rec["id"], "--harmful")["ok"] is True
    assert "restamped" in run("verify", rec["id"])
    assert run("status")["counts"] == {"convention": 1}
    assert run("retract", rec["id"], "--reason", "wrong")["ok"] is True
    assert run("status")["total_active"] == 0


def test_service_errors_exit_2_with_a_machine_readable_body(run):
    out = run("remember", "module", "-F", "path=x", expect=2)
    assert out["error"]["code"] == "unknown_kind"
    out = run("recall", "t", "-f", "../escape.py", expect=2)
    assert out["error"]["code"] == "path_outside_repo"
    plain = run("feedback", "nope", "--helpful", json_out=False, expect=2)
    assert "not_found" in plain


def test_field_syntax_errors_are_usage_errors(repo):
    result = CliRunner().invoke(main, ["remember", "convention", "-F", "no-equals-sign",
                                       "--repo", str(repo)])
    assert result.exit_code == 2 and "key=value" in result.output


def test_session_commands_span_invocations(run, repo):
    started = run("session", "start", "add retries")
    sid = started["session_id"]
    assert started["already_active"] is False
    run("remember", "open_question", "-F", "question=cap the retries?", "--session", sid)
    ended = run("session", "end", sid, "--summary", "added notes")
    assert ended["session_id"] == sid
    assert any("cap the retries" in i["text"] for i in ended["unresolved"])
    run("session", "end", "no-such", expect=2)


def test_priority_must_needs_the_flag(run):
    out = run("remember", "convention", "-F", "category=c", "-F", "rule=pinned", "--priority", "must",
              expect=2)
    assert out["error"]["code"] == "invalid_argument"
    ok = run("remember", "convention", "-F", "category=c", "-F", "rule=pinned", "--priority", "must",
             "--allow-must")
    assert ok["created"] is True


def test_serve_command_exists_and_documents_itself():
    result = CliRunner().invoke(main, ["serve", "--help"])
    assert result.exit_code == 0 and "MCP" in result.output


# -- recall --context --------------------------------------------------------

TRACE = ("Traceback (most recent call last):\n"
         '  File "/srv/app/payments/hooks.py", line 31, in receive\n'
         "    event = construct_event(payload, header)\n"
         "StripeSignatureError: No signatures found matching the expected signature\n")


def _remember_stripe(run):
    return run("remember", "convention", "-F", "category=payments",
               "-F", "rule=StripeSignatureError means the endpoint secret rotated; reload it")


def test_recall_context_lifts_a_note_the_task_alone_misses(run):
    remembered = _remember_stripe(run)
    alone = run("recall", "the checkout suite is red again")
    assert alone["items"] == []                                  # the task alone abstains
    lifted = run("recall", "the checkout suite is red again", "--context", TRACE)
    assert [i["id"] for i in lifted["items"]] == [remembered["id"]]


def test_recall_context_dash_reads_stdin(run, repo):
    remembered = _remember_stripe(run)
    result = CliRunner().invoke(main, ["recall", "the checkout suite is red again",
                                       "--context", "-", "--repo", str(repo), "--json"],
                                input=TRACE)
    assert result.exit_code == 0, result.output
    assert [i["id"] for i in json.loads(result.output)["items"]] == [remembered["id"]]


def test_the_cli_protocol_block_mentions_recall_context():
    from agent_memory.protocol import block
    assert "--context" in block("cli") and "context" in block("mcp")


def test_recall_context_too_long_is_a_service_error(run):
    out = run("recall", "t", "--context", "x" * 2001, expect=2)
    assert out["error"]["code"] == "too_large"


def test_recall_does_not_read_stdin_unless_asked(run, repo):
    _remember_stripe(run)
    result = CliRunner().invoke(main, ["recall", "the checkout suite is red again",
                                       "--repo", str(repo), "--json"], input=TRACE)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["items"] == []              # stdin was left alone


# -- errors ------------------------------------------------------------------

def test_an_unexpected_failure_is_a_generic_internal_error(run, repo, monkeypatch):
    from agent_memory.service import MemoryService
    secret = str(repo / "secret" / "path.py")

    def boom(self):
        raise RuntimeError(f"failed reading {secret}")

    monkeypatch.setattr(MemoryService, "status", boom)
    out = run("status", expect=2)
    assert out["error"]["code"] == "internal"
    plain = run("status", json_out=False, expect=2)
    for text in (json.dumps(out), plain):
        assert "secret" not in text and "Traceback" not in text and "RuntimeError" not in text


def test_a_dropped_note_is_reported(run, monkeypatch):
    from agent_memory.service import MemoryService

    def recall(self, task, files=None, symbols=None, max_tokens=None, context=None):
        return {"items": [{"id": "c1"}], "prompt": "PROMPT-TEXT", "tokens": 3,
                "note": "2 more relevant note(s) did not fit"}

    monkeypatch.setattr(MemoryService, "recall", recall)
    assert run("recall", "t")["note"] == "2 more relevant note(s) did not fit"
    plain = run("recall", "t", json_out=False)
    assert "PROMPT-TEXT" in plain and "did not fit" in plain


# -- the legacy commands ---------------------------------------------------

def test_legacy_write_commands_work_on_a_store_opened_with_open(tmp_path):
    path = str(tmp_path / "legacy.memory")
    AgentMemory.open(path).close()                  # a shared store: has a journal beside it
    runner = CliRunner()
    result = runner.invoke(main, ["add", "convention", path, "-c", "naming", "-r", "snake_case only"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["start-session", path, "legacy task"])
    assert result.exit_code == 0, result.output
    stats = runner.invoke(main, ["stats", path])
    assert stats.exit_code == 0, stats.output
    loaded = AgentMemory.load(path)
    assert any("snake_case only" in n.properties.get("rule", "") for n in loaded.graph.nodes("convention"))


def test_legacy_add_decision_and_bug_pattern_write(tmp_path):
    # `list` is the click group inside cli.py, so these commands must not call list(...)
    path = str(tmp_path / "legacy.memory")
    AgentMemory.open(path).close()
    runner = CliRunner()
    result = runner.invoke(main, ["add", "decision", path, "-t", "use sqlite", "-c", "small",
                                  "-d", "alice"])
    assert result.exit_code == 0 and "Added decision" in result.output, result.output
    result = runner.invoke(main, ["add", "bug-pattern", path, "-n", "retry_storm",
                                  "-s", "unbounded retries", "-m", "net"])
    assert result.exit_code == 0 and "Added bug pattern" in result.output, result.output
    loaded = AgentMemory.load(path)
    assert [n.properties.get("title") for n in loaded.graph.nodes("decision")] == ["use sqlite"]
    assert len([*loaded.graph.nodes("bug_pattern")]) == 1


def test_legacy_write_on_a_missing_file_fails_and_creates_nothing(tmp_path):
    path = tmp_path / "typo.memory"
    result = CliRunner().invoke(main, ["add", "convention", str(path), "-c", "a", "-r", "b"])
    assert result.exit_code == 1 and "No such file" in result.output
    assert list(tmp_path.iterdir()) == []


def test_legacy_commands_still_work_on_a_plain_file(tmp_path):
    path = str(tmp_path / "plain.memory")
    runner = CliRunner()
    assert runner.invoke(main, ["init", "demo", "-o", path]).exit_code == 0
    assert runner.invoke(main, ["add", "convention", path, "-c", "naming", "-r", "x is y"]).exit_code == 0
    assert runner.invoke(main, ["check", path, "use camelCase"]).exit_code == 0   # legacy 2-arg form
    assert runner.invoke(main, ["list", "conventions", path]).exit_code == 0


# -- fix round 1 ---------------------------------------------------------------

import io            # noqa: E402
import os            # noqa: E402
import subprocess    # noqa: E402
import sys           # noqa: E402

from agent_memory.protocol import block   # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _amem(repo, *args):
    """A real `amem` process with stdout piped and a narrow (cp1252) output encoding."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONPATH"] = os.pathsep.join([SRC, env.get("PYTHONPATH", "")])
    env["PYTHONIOENCODING"] = "cp1252"
    return subprocess.run([sys.executable, "-m", "agent_memory.cli", *args, "--repo", str(repo)],
                          stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=60)


def test_human_output_survives_characters_the_terminal_cannot_encode(run, repo):
    run("remember", "convention", "-F", "category=naming",
        "-F", "rule=cache keys use colon namespaces \u2192 \u65e5\u672c")
    plain = _amem(repo, "recall", "how should I name cache keys")
    assert plain.returncode == 0, plain.stderr.decode("cp1252", "replace")
    out = plain.stdout.decode("cp1252")
    assert "colon namespaces" in out and "Traceback" not in plain.stderr.decode("cp1252", "replace")
    as_json = _amem(repo, "recall", "how should I name cache keys", "--json")
    assert as_json.returncode == 0
    texts = [i["text"] for i in json.loads(as_json.stdout.decode("ascii"))["items"]]
    assert any("\u2192 \u65e5\u672c" in t for t in texts)        # JSON is lossless, as before


def test_recall_context_stdin_tolerates_invalid_utf8(run, repo):
    _remember_stripe(run)
    result = CliRunner().invoke(main, ["recall", "the checkout suite is red again",
                                       "--context", "-", "--repo", str(repo), "--json"],
                                input=b"abc \xff\xfe\x80 StripeSignatureError")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["items"]


def test_recall_context_stdin_accepts_a_full_2000_character_context(run, repo):
    result = CliRunner().invoke(main, ["recall", "t", "--context", "-", "--repo", str(repo),
                                       "--json"], input=("\U0001F600" * 2000).encode("utf-8"))
    assert result.exit_code == 0, result.output


class _Endless(io.RawIOBase):
    """A 1 MB stdin that counts how much of it was read."""

    def __init__(self, size=1 << 20):
        super().__init__()
        self.left = size
        self.read_bytes = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        n = min(len(buffer), self.left)
        buffer[:n] = b"x" * n
        self.left -= n
        self.read_bytes += n
        return n


def test_recall_context_stdin_is_bounded(run, repo):
    stream = _Endless()
    result = CliRunner().invoke(main, ["recall", "t", "--context", "-", "--repo", str(repo),
                                       "--json"], input=stream)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["error"]["code"] == "too_large"
    assert stream.read_bytes <= 16 * 1024                       # never the whole megabyte


def test_the_protocol_session_flow_attaches_writes_to_the_session(run, repo):
    text = block("cli")
    start_cmd = 'amem session start "<one-line task>"'
    remember_cmd = "amem remember <kind> -F key=value ... --session <session-id>"
    end_cmd = "amem session end <session-id>"
    for command in (start_cmd, remember_cmd, end_cmd):
        assert command in text, command
    # exactly as written: start, remember with --session, end
    sid = run("session", "start", "add retries")["session_id"]
    run("remember", "open_question", "-F", "question=cap the retries?", "--session", sid)
    ended = run("session", "end", sid)
    assert any("cap the retries" in i["text"] for i in ended["unresolved"])
    # a session that never ended (the agent crashed): the next start carries its open work
    crashed = run("session", "start", "tune backoff")["session_id"]
    run("remember", "blocker", "-F", "description=backoff ceiling unknown", "--session", crashed)
    resumed = run("session", "start", "tune backoff again")
    assert resumed["session_id"] != crashed
    assert any("backoff ceiling unknown" in i["text"] for i in resumed["carried"])


def test_duplicate_field_keys_are_usage_errors(repo):
    result = CliRunner().invoke(main, ["remember", "convention", "-F", "category=a",
                                       "-F", "rule=x", "-F", "rule=y", "--repo", str(repo)])
    assert result.exit_code == 2 and "rule" in result.output and "more than once" in result.output


def test_legacy_write_commands_still_work_on_an_isonl_file(tmp_path):
    # a shared store cannot be .isonl, so these keep the old load/save path
    path = str(tmp_path / "plain.isonl")
    runner = CliRunner()
    assert runner.invoke(main, ["init", "demo", "-o", path]).exit_code == 0
    result = runner.invoke(main, ["add", "convention", path, "-c", "naming", "-r", "x is y"])
    assert result.exit_code == 0 and "Added convention" in result.output, result.output
    result = runner.invoke(main, ["start-session", path, "isonl task"])
    assert result.exit_code == 0, result.output
    loaded = AgentMemory.load(path)
    assert [n.properties.get("rule") for n in loaded.graph.nodes("convention")] == ["x is y"]


# -- startup errors and terminal controls in human output ---------------------

def test_serve_startup_service_error_is_a_clean_exit_2(repo, monkeypatch):
    import sys
    import types
    from agent_memory.service import MemoryService
    fake = types.ModuleType("agent_memory.mcp_server")
    fake.serve = lambda config: MemoryService(config)          # what the real serve does first
    monkeypatch.setitem(sys.modules, "agent_memory.mcp_server", fake)
    result = CliRunner().invoke(main, ["serve", "--store", str(repo / "x.isonl"),
                                       "--repo", str(repo)])
    assert result.exit_code == 2, result.output
    assert "error [invalid_argument]:" in result.output and "Traceback" not in result.output


def test_serve_startup_service_error_with_the_real_server(repo):
    pytest.importorskip("mcp")
    result = CliRunner().invoke(main, ["serve", "--store", str(repo / "x.isonl"),
                                       "--repo", str(repo)])
    assert result.exit_code == 2, result.output
    assert "error [invalid_argument]:" in result.output


def test_remember_human_output_drops_terminal_controls(run, monkeypatch):
    from agent_memory.service import MemoryService

    def remember(self, kind, fields, files=None, evidence="", confidence=None, priority=None):
        return {"id": "c1\x1b[2J", "kind": kind, "created": True, "merged": False,
                "similar": ["c2\x1b]0;owned\x07", "c3\x9b31m"]}

    monkeypatch.setattr(MemoryService, "remember", remember)
    text = run("remember", "convention", "-F", "category=a", "-F", "rule=b", json_out=False)
    assert "c1" in text and "c2" in text and "c3" in text
    for bad in ("\x1b", "\x07", "\x9b"):
        assert bad not in text


def test_status_human_output_drops_terminal_controls(run, monkeypatch):
    from agent_memory.service import MemoryService

    def status(self):
        return {"store": "s", "repo_root": "r", "total_active": 0, "last_write": None,
                "counts": {}, "warnings": ["journal damaged\x1b[31m\rFAKE OK\x07\ttab"]}

    monkeypatch.setattr(MemoryService, "status", status)
    text = run("status", json_out=False)
    assert "journal damaged" in text and "\ttab" in text
    for bad in ("\x1b", "\x07", "\r"):
        assert bad not in text
