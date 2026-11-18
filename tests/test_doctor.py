import json

import pytest
from click.testing import CliRunner

from agent_memory.cli import main
from agent_memory.doctor import run_doctor
from agent_memory.installer.changes import apply_changes
from agent_memory.installer.plan import plan_install
from agent_memory.service import MemoryService, resolve_config


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def by_name(checks):
    return {c["check"]: c for c in checks}


def test_a_fresh_repo(repo):
    checks = by_name(run_doctor(repo))
    assert checks["python"]["ok"] and "will be created" in checks["store"]["detail"]
    assert "opens" not in checks
    assert all(checks[f"tool:{t}"]["detail"] == "not installed"
               for t in ("claude-code", "cursor", "codex", "devin", "generic"))
    assert all(c["ok"] for c in checks.values())


def test_a_working_store_and_an_installed_tool(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    service.remember("convention", {"category": "c", "rule": "r"})
    service.close()
    apply_changes(plan_install("claude-code", repo).changes)
    checks = by_name(run_doctor(repo))
    assert checks["opens"]["ok"] and "1 active" in checks["opens"]["detail"]
    assert checks["tool:claude-code"]["detail"] == "installed"
    assert checks["tool:cursor"]["detail"] == "not installed"


def test_a_corrupt_store_is_reported_not_raised(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    service.remember("convention", {"category": "c", "rule": "r"})
    store = service.config.store_path
    service.memory.close()
    with open(store, "w") as handle:
        handle.write("not a memory file")
    checks = by_name(run_doctor(repo))
    assert checks["opens"]["ok"] is False and "store_corrupt" in checks["opens"]["detail"]


def test_a_half_installed_tool_is_not_reported_as_installed(repo):
    apply_changes([c for c in plan_install("claude-code", repo).changes if c.path.name == "CLAUDE.md"])
    assert by_name(run_doctor(repo))["tool:claude-code"]["detail"] == "not installed"


def test_doctor_command_exit_codes(repo):
    runner = CliRunner()
    ok = runner.invoke(main, ["doctor", "--repo", str(repo), "--json"])
    assert ok.exit_code == 0 and json.loads(ok.output)["ok"] is True
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    service.remember("convention", {"category": "c", "rule": "r"})
    store = service.config.store_path
    service.memory.close()
    with open(store, "w") as handle:
        handle.write("not a memory file")
    bad = runner.invoke(main, ["doctor", "--repo", str(repo)])
    assert bad.exit_code == 1 and "store_corrupt" in bad.output


# -- beyond the plan: a diagnostic must not change what it inspects, nor leak --------------

def test_a_plain_store_is_read_and_not_upgraded(repo):
    """Opening a plain (legacy) .memory file through the service converts it in place to a
    shared store, one way. The doctor reads it instead and says so."""
    from pathlib import Path

    from agent_memory import AgentMemory
    store = repo / ".agent-memory" / "project.memory"
    store.parent.mkdir()
    memory = AgentMemory("project", repo_root=str(repo))
    memory.remember("convention", {"category": "c", "rule": "r"})
    memory.save(str(store))
    before = store.read_bytes()
    checks = by_name(run_doctor(repo))
    assert checks["opens"]["ok"] and "1 active" in checks["opens"]["detail"]
    assert "plain" in checks["opens"]["detail"]
    assert store.read_bytes() == before
    assert not Path(str(store) + ".journal").exists() and not Path(str(store) + ".lock").exists()


def test_an_isonl_store_is_refused(repo, tmp_path):
    checks = by_name(run_doctor(repo, str(tmp_path / "project.isonl")))
    assert checks["store"]["ok"] is False and ".isonl" in checks["store"]["detail"]
    assert "opens" not in checks


def test_a_missing_repo_directory_fails(tmp_path):
    checks = by_name(run_doctor(tmp_path / "nowhere"))
    assert checks["repo"]["ok"] is False


def test_mcp_absent_is_only_a_warning(repo, monkeypatch):
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "mcp" else real(name, *a))
    check = by_name(run_doctor(repo))["mcp"]
    assert check["ok"] is True and check["detail"].startswith("not installed")


def test_the_doctor_module_does_not_import_mcp():
    from storage_helpers import run_py
    out = run_py("import sys, agent_memory.doctor; print('mcp' in sys.modules)")
    assert out.returncode == 0 and out.stdout.strip() == "False", out.stderr


def test_human_output_drops_terminal_controls(repo, monkeypatch):
    import agent_memory.doctor as doctor
    monkeypatch.setattr(doctor, "run_doctor", lambda root, store=None: [
        {"check": "warnings", "ok": False, "detail": "journal \x1b[2J\x1b]0;x\x07damaged"}])
    out = CliRunner().invoke(main, ["doctor", "--repo", str(repo)])
    assert out.exit_code == 1 and "\x1b" not in out.output and "\x07" not in out.output
    assert "journal [2J]0;xdamaged" in out.output


def test_an_unexpected_failure_is_one_line_not_a_traceback(repo, monkeypatch):
    import agent_memory.doctor as doctor

    def boom(root, store=None):
        raise RuntimeError("C:/secret/path token=abc")
    monkeypatch.setattr(doctor, "run_doctor", boom)
    for args in ([], ["--json"]):
        out = CliRunner().invoke(main, ["doctor", "--repo", str(repo), *args])
        assert out.exit_code == 1 and isinstance(out.exception, SystemExit), out.output
        assert "Traceback" not in out.output and "secret" not in out.output
        assert "doctor failed unexpectedly" in out.output
