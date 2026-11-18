"""Per-tool install plans, the `amem install` command and the golden files.

Regenerate the golden files (then read every one before committing them):
    UPDATE_GOLDEN=1 python -m pytest tests/test_installer_tools.py -q -k golden
"""
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from agent_memory.cli import main
from agent_memory.installer import InstallError
from agent_memory.installer.changes import apply_changes
from agent_memory.installer.markers import BEGIN, END
from agent_memory.installer.plan import plan_install
from agent_memory.installer.tools import TOOLS
from agent_memory.protocol import block

GOLDEN = Path(__file__).parent / "golden" / "install"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _apply(plan, repo):
    return apply_changes(plan.changes, root=str(repo))


def _files(repo):
    return {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}


def test_the_supported_tools():
    assert set(TOOLS) == {"claude-code", "cursor", "codex", "devin", "generic"}


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_dry_run_plans_but_writes_nothing(tool, repo):
    plan = plan_install(tool, repo)
    assert plan.changes and all(c.status == "create" or c.status == "update" for c in plan.changes)
    assert [p.name for p in repo.iterdir()] == [".git"]


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_install_is_idempotent(tool, repo):
    _apply(plan_install(tool, repo), repo)
    snapshot = _files(repo)
    second = plan_install(tool, repo)
    assert all(c.status == "unchanged" for c in second.changes)
    _apply(second, repo)
    assert snapshot == _files(repo)


def test_user_content_survives(repo):
    (repo / "CLAUDE.md").write_text("# House rules\n\nNever touch prod.\n", encoding="utf-8")
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"git": {"command": "git-mcp"}}}))
    _apply(plan_install("claude-code", repo), repo)
    text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.startswith("# House rules\n\nNever touch prod.\n") and BEGIN in text
    servers = json.loads((repo / ".mcp.json").read_text())["mcpServers"]
    assert servers["git"] == {"command": "git-mcp"} and servers["agent-memory"]["args"] == ["serve"]


def test_crlf_instruction_file_stays_crlf(repo):
    (repo / "AGENTS.md").write_bytes(b"# Rules\r\n\r\nBe kind.\r\n")
    _apply(plan_install("codex", repo), repo)
    raw = (repo / "AGENTS.md").read_bytes().decode("utf-8")
    assert raw.count("\n") == raw.count("\r\n") and BEGIN in raw
    assert raw.startswith("# Rules\r\n\r\nBe kind.\r\n")


def test_an_invalid_existing_mcp_config_is_refused_and_left_alone(repo):
    (repo / ".mcp.json").write_text("{not json")
    with pytest.raises(InstallError) as caught:
        plan_install("claude-code", repo)
    assert ".mcp.json" in str(caught.value)                       # the error names the file
    assert (repo / ".mcp.json").read_text() == "{not json"


def test_mcp_entry_is_machine_independent_unless_a_store_is_given(repo):
    default = plan_install("claude-code", repo)
    entry = json.loads(next(c.new for c in default.changes if c.path.name == ".mcp.json"))
    assert entry["mcpServers"]["agent-memory"] == {"command": "amem", "args": ["serve"]}
    explicit = plan_install("claude-code", repo, store=str(repo / "x.memory"), command="py-amem")
    entry = json.loads(next(c.new for c in explicit.changes if c.path.name == ".mcp.json"))
    assert entry["mcpServers"]["agent-memory"]["command"] == "py-amem"
    assert "--store" in entry["mcpServers"]["agent-memory"]["args"]


def test_gitignore_only_in_a_git_repo(repo, tmp_path):
    plan = plan_install("codex", repo)
    assert any(c.path.name == ".gitignore" and ".agent-memory/" in c.new for c in plan.changes)
    bare = tmp_path / "not-git"
    bare.mkdir()
    assert not any(c.path.name == ".gitignore" for c in plan_install("codex", bare).changes)


def test_gitignore_entry_is_added_once_across_installs_and_tools(repo):
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    for tool in ("codex", "claude-code", "cursor", "codex"):
        _apply(plan_install(tool, repo), repo)
    lines = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines == ["node_modules/", ".agent-memory/"]


def test_command_only_tools_print_the_command_and_write_no_mcp_file(repo):
    plan = plan_install("codex", repo)
    assert any(cmd.startswith("codex mcp add agent-memory") for cmd in plan.commands)
    assert not any(c.path.suffix == ".json" for c in plan.changes)
    assert plan_install("generic", repo).commands == []


def test_devin_is_flagged_as_unverified(repo):
    notes = " ".join(plan_install("devin", repo).notes)
    assert "secondary sources" in notes and "not written for you" in notes


def test_cursor_rule_gets_front_matter_only_on_creation(repo):
    _apply(plan_install("cursor", repo), repo)
    path = repo / ".cursor" / "rules" / "agent-memory.mdc"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\ndescription: Project memory protocol\nalwaysApply: true\n---\n")
    assert text.count("alwaysApply") == 1
    _apply(plan_install("cursor", repo), repo)
    assert path.read_text(encoding="utf-8").count("alwaysApply") == 1


def test_generic_uses_the_cli_block_and_the_others_the_mcp_block(repo):
    assert block("cli") in next(c.new for c in plan_install("generic", repo).changes
                                if c.path.name == "AGENTS.md")
    assert block("mcp") in next(c.new for c in plan_install("claude-code", repo).changes
                                if c.path.name == "CLAUDE.md")


def test_two_edits_to_one_file_become_one_change(repo, monkeypatch):
    from agent_memory.installer import plan as plan_module
    from agent_memory.installer.tools import ToolSpec
    odd = dict(TOOLS, odd=ToolSpec("odd", "Odd", ".gitignore", "cli"))
    monkeypatch.setattr(plan_module, "TOOLS", odd)
    plan = plan_install("odd", repo)
    assert [c.path.name for c in plan.changes] == [".gitignore"]
    text = plan.changes[0].new
    assert BEGIN in text and text.count(".agent-memory/") == 1
    _apply(plan, repo)                                          # apply_changes accepts it


def test_a_missing_repo_directory_is_refused(tmp_path):
    with pytest.raises(InstallError):
        plan_install("codex", tmp_path / "nope")
    assert not (tmp_path / "nope").exists()


def test_an_unknown_tool_is_refused():
    with pytest.raises(InstallError):
        plan_install("emacs", ".")


# -- the command -----------------------------------------------------------

def test_install_command_dry_run_then_write(repo):
    runner = CliRunner()
    dry = runner.invoke(main, ["install", "claude-code", "--repo", str(repo)])
    assert dry.exit_code == 0 and "Dry run" in dry.output and "+++" in dry.output
    assert not (repo / "CLAUDE.md").exists()
    wrote = runner.invoke(main, ["install", "claude-code", "--repo", str(repo), "--write"])
    assert wrote.exit_code == 0 and (repo / "CLAUDE.md").exists() and (repo / ".mcp.json").exists()
    again = runner.invoke(main, ["install", "claude-code", "--repo", str(repo), "--write"])
    assert "nothing to change" in again.output.lower()


def test_install_command_json_and_errors(repo):
    runner = CliRunner()
    out = runner.invoke(main, ["install", "codex", "--repo", str(repo), "--json"])
    body = json.loads(out.output)
    assert body["written"] is False and body["changes"] and body["commands"]
    (repo / ".mcp.json").write_text("{bad")
    bad = runner.invoke(main, ["install", "claude-code", "--repo", str(repo)])
    # an InstallError exits 1 (the brief said 2; the carried review fact wins)
    assert bad.exit_code == 1 and "not valid JSON" in bad.output and ".mcp.json" in bad.output
    assert "Traceback" not in bad.output
    unknown = runner.invoke(main, ["install", "emacs", "--repo", str(repo)])
    assert unknown.exit_code == 2


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_install_command_write_twice_is_byte_identical(tool, repo):
    (repo / "AGENTS.md").write_bytes(b"# Ours\r\n\r\nKeep this.\r\n")
    (repo / "CLAUDE.md").write_bytes(b"# House rules\n\nNever touch prod.\n")
    runner = CliRunner()
    first = runner.invoke(main, ["install", tool, "--repo", str(repo), "--write"])
    assert first.exit_code == 0, first.output
    snapshot = _files(repo)
    second = runner.invoke(main, ["install", tool, "--repo", str(repo), "--write"])
    assert second.exit_code == 0 and "nothing to change" in second.output.lower()
    assert snapshot == _files(repo)
    assert snapshot[repo / "AGENTS.md"].startswith(b"# Ours\r\n\r\nKeep this.\r\n")
    assert snapshot[repo / "CLAUDE.md"].startswith(b"# House rules\n\nNever touch prod.\n")


def test_install_command_refuses_invalid_json_in_json_mode_and_writes_nothing(repo):
    (repo / ".mcp.json").write_text("{bad")
    out = CliRunner().invoke(main, ["install", "claude-code", "--repo", str(repo), "--write",
                                    "--json"])
    assert out.exit_code == 1
    body = json.loads(out.output)
    assert "not valid JSON" in body["error"]["message"] and ".mcp.json" in body["error"]["message"]
    assert (repo / ".mcp.json").read_text() == "{bad" and not (repo / "CLAUDE.md").exists()


def test_install_command_shows_an_existing_entry_being_updated(repo):
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"agent-memory": {
        "command": "old-amem", "args": ["serve"], "env": {"A": "1"}}}}, indent=2) + "\n")
    out = CliRunner().invoke(main, ["install", "claude-code", "--repo", str(repo)])
    assert out.exit_code == 0
    assert '-      "command": "old-amem"' in out.output and '+      "command": "amem"' in out.output
    CliRunner().invoke(main, ["install", "claude-code", "--repo", str(repo), "--write"])
    entry = json.loads((repo / ".mcp.json").read_text())["mcpServers"]["agent-memory"]
    assert entry == {"command": "amem", "args": ["serve"], "env": {"A": "1"}}


def test_install_command_reports_a_mixed_line_ending_file_cleanly(repo):
    (repo / "CLAUDE.md").write_bytes(b"a\r\nb\n")
    out = CliRunner().invoke(main, ["install", "claude-code", "--repo", str(repo), "--write"])
    assert out.exit_code == 1 and "mixes line endings" in out.output
    assert "Traceback" not in out.output and not (repo / ".mcp.json").exists()


def test_install_command_unexpected_failure_is_generic(repo, monkeypatch):
    from agent_memory.installer import plan as plan_module
    secret = str(repo / "secret" / "path")

    def boom(*args, **kwargs):
        raise RuntimeError(f"failed at {secret}")

    monkeypatch.setattr(plan_module, "plan_install", boom)
    out = CliRunner().invoke(main, ["install", "codex", "--repo", str(repo)])
    assert out.exit_code == 1
    assert "secret" not in out.output and "Traceback" not in out.output


def test_install_command_dry_run_strips_terminal_controls_from_user_text(repo):
    (repo / "CLAUDE.md").write_text("# Rules\x1b]0;owned\x07\n", encoding="utf-8")
    out = CliRunner().invoke(main, ["install", "claude-code", "--repo", str(repo)])
    assert out.exit_code == 0 and "\x1b" not in out.output and "\x07" not in out.output


# -- golden files ----------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_output_matches_the_golden_files(tool, repo):
    plan = plan_install(tool, repo)
    produced = {c.path.relative_to(repo).as_posix(): c.new for c in plan.changes}
    produced["COMMANDS.txt"] = "\n".join(plan.commands) + ("\n" if plan.commands else "")
    produced["NOTES.txt"] = "\n".join(plan.notes) + ("\n" if plan.notes else "")
    directory = GOLDEN / tool
    if os.environ.get("UPDATE_GOLDEN") == "1":
        for relative, text in produced.items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
    expected = {p.relative_to(directory).as_posix(): p.read_text(encoding="utf-8")
                for p in directory.rglob("*") if p.is_file()}
    assert produced == expected
