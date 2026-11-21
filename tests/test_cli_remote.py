import json

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
def needs_mcp():
    """`serve --http` loads the remote server, which needs the optional `mcp` package. Every test
    using this is rejected before uvicorn runs: none may ever start a server."""
    pytest.importorskip("mcp")


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


def test_serve_http_refuses_to_start_with_no_token(needs_mcp, amem):
    out = amem("serve", "--http", expect=2)
    assert "amem token create" in out


def test_serve_http_refuses_a_public_bind_without_the_tls_flag(needs_mcp, amem):
    amem("token", "create", "alice", "--role", "read-write")
    out = amem("serve", "--http", "--host", "0.0.0.0", expect=2)
    assert "--behind-tls" in out


def test_serve_http_refuses_stdio_only_options(needs_mcp, amem):
    amem("token", "create", "alice", "--role", "read-write")
    out = amem("serve", "--http", "--allow-must", expect=2)
    assert "allow-must" in out
    out = amem("serve", "--agent", "x", "--http", expect=2)
    assert "agent" in out


def test_http_only_options_need_http(amem):
    out = amem("serve", "--port", "9000", expect=2)
    assert "--http" in out


def test_create_still_shows_the_token_when_the_file_cannot_be_read_back(amem, repo, monkeypatch):
    def unreadable(self):
        raise OSError("the disk went away")

    monkeypatch.setattr(TokenFile, "entries", unreadable)
    out = amem("token", "create", "alice", "--role", "read-write", "--expires-in", "30")
    token = next(word for word in out.split() if word.startswith("amt_"))
    assert "shown once" in out and "Expires:         20" in out
    monkeypatch.undo()
    assert TokenFile(repo / ".agent-memory" / "tokens.json").verify(token).name == "alice"


def test_create_json_survives_a_re_read_that_finds_nothing(amem, monkeypatch):
    monkeypatch.setattr(TokenFile, "entries", lambda self: [])
    made = amem("token", "create", "alice", "--role", "read-only", "--expires-in", "30", json_out=True)
    assert made["token"].startswith("amt_") and made["expires_at"].startswith("20")


@pytest.fixture
def recorded_run(monkeypatch):
    """Replace the real server runner: a test using it can never start uvicorn."""
    pytest.importorskip("mcp")
    from agent_memory.remote import server
    calls = []
    monkeypatch.setattr(server, "run_http", lambda *args, **kwargs: calls.append(args))
    return calls


def test_port_zero_means_port_zero_not_the_default(amem, recorded_run):
    amem("serve", "--http", "--port", "0")
    assert recorded_run[0][2:4] == ("127.0.0.1", 0)


def test_the_defaults_apply_when_the_options_are_absent(amem, recorded_run):
    amem("serve", "--http")
    assert recorded_run[0][2:4] == ("127.0.0.1", 8765)


def test_an_empty_host_is_refused_not_turned_into_loopback(amem, recorded_run):
    out = amem("serve", "--http", "--host", "", expect=2)
    assert "--host" in out and recorded_run == []


def test_an_empty_tokens_path_is_refused(amem, recorded_run):
    out = amem("serve", "--http", "--tokens", "", expect=2)
    assert "--tokens" in out and recorded_run == []


def test_http_only_options_with_falsy_values_still_need_http(amem):
    assert "--http" in amem("serve", "--port", "0", expect=2)
    assert "--http" in amem("serve", "--host", "", expect=2)
    assert "--http" in amem("serve", "--public-url", "", expect=2)
