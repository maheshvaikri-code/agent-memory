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


# -- fix round 1: one failing check never drops the others; orphaned journals are found -----

TOOL_CHECKS = {f"tool:{t}" for t in ("claude-code", "cursor", "codex", "devin", "generic")}


def test_a_store_path_that_is_a_directory_is_reported_and_the_rest_still_run(repo, tmp_path):
    store = tmp_path / "dir.memory"
    store.mkdir()
    checks = by_name(run_doctor(repo, str(store)))
    assert any(not c["ok"] for c in checks.values())
    assert TOOL_CHECKS <= set(checks) and "python" in checks
    out = CliRunner().invoke(main, ["doctor", "--repo", str(repo), "--store", str(store)])
    assert out.exit_code == 1 and "Traceback" not in out.output
    assert "tool:cursor" in out.output


def test_an_unreadable_plain_store_is_a_failed_check_not_a_crash(repo, monkeypatch):
    import agent_memory.doctor as doctor
    from agent_memory import AgentMemory
    store = repo / ".agent-memory" / "project.memory"
    store.parent.mkdir()
    AgentMemory("project", repo_root=str(repo)).save(str(store))

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(doctor.AgentMemory, "load", denied)
    checks = by_name(run_doctor(repo))
    assert checks["opens"]["ok"] is False and "store_unreadable" in checks["opens"]["detail"]
    assert TOOL_CHECKS <= set(checks) and "lock" in checks
    out = CliRunner().invoke(main, ["doctor", "--repo", str(repo)])
    assert out.exit_code == 1 and "store_unreadable" in out.output


def test_an_unexpected_failure_in_one_check_fails_only_that_check(repo, monkeypatch):
    import agent_memory.doctor as doctor

    def boom(root, key):
        raise RuntimeError("C:/secret token=abc")
    monkeypatch.setattr(doctor, "_tool_installed", boom)
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    service.remember("convention", {"category": "c", "rule": "r"})
    service.close()
    checks = by_name(run_doctor(repo))
    assert checks["opens"]["ok"] is True
    assert all(checks[t]["ok"] is False for t in TOOL_CHECKS)
    assert not any("secret" in c["detail"] for c in checks.values())


def test_a_journal_without_its_snapshot_is_corrupt_not_healthy(repo):
    service = MemoryService(resolve_config(repo=str(repo), env={}))
    service.remember("convention", {"category": "c", "rule": "r"})
    store = service.config.store_path
    import os
    from agent_memory.storage import journal
    assert journal.read_tail(store + ".journal", 0).txs     # the write is in the journal only
    os.remove(store)                             # not closed: close() would fold the journal
    checks = by_name(run_doctor(repo))
    assert checks["opens"]["ok"] is False and "store_corrupt" in checks["opens"]["detail"]
    assert not os.path.exists(store)             # the doctor did not start a new store over it
    out = CliRunner().invoke(main, ["doctor", "--repo", str(repo)])
    assert out.exit_code == 1 and "store_corrupt" in out.output


def test_leftover_lock_and_empty_journal_without_a_snapshot_are_not_an_error(repo):
    import os
    store = repo / ".agent-memory" / "project.memory"
    store.parent.mkdir()
    (store.parent / "project.memory.lock").write_text("")
    (store.parent / "project.memory.journal").write_text("")
    checks = by_name(run_doctor(repo))
    assert all(c["ok"] for c in checks.values()), checks
    assert "will be created" in checks["store"]["detail"]
    assert not os.path.exists(store)
