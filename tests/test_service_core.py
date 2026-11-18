import os
import threading

import pytest

from agent_memory import AgentMemory
from agent_memory.service import (AGENT_KINDS, MemoryService, ServiceConfig, ServiceError,
                                  resolve_config, sanitize_agent)
from agent_memory.storage import journal


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def svc(repo):
    service = MemoryService(resolve_config(repo=str(repo), agent="tester", env={}))
    yield service
    service.close()


def code_of(excinfo):
    return excinfo.value.code


CONVENTION = {"category": "naming", "rule": "cache keys use colon namespaces"}


# -- configuration ---------------------------------------------------------

def test_default_store_and_repo(repo):
    config = resolve_config(repo=str(repo), env={})
    assert config.repo_root == str(repo.resolve())
    assert config.store_path == str(repo.resolve() / ".agent-memory" / "project.memory")
    assert config.agent == "cli" and config.allow_must is False


def test_precedence_arguments_over_environment(repo, tmp_path):
    env = {"AGENT_MEMORY_STORE": str(tmp_path / "env.memory"), "AGENT_MEMORY_REPO": str(repo),
           "AGENT_MEMORY_AGENT": "from-env"}
    assert resolve_config(env=env).store_path == str(tmp_path / "env.memory")
    assert resolve_config(env=env).agent == "from-env"
    explicit = resolve_config(store=str(tmp_path / "arg.memory"), agent="arg", env=env)
    assert explicit.store_path == str(tmp_path / "arg.memory") and explicit.agent == "arg"


def test_repo_root_is_the_git_toplevel_above_the_working_directory(repo):
    deep = repo / "src" / "pkg"
    deep.mkdir()
    assert resolve_config(env={}, cwd=str(deep)).repo_root == str(repo.resolve())


def test_agent_names_are_sanitised():
    assert sanitize_agent("Claude Code/1.0") == "Claude-Code-1.0"
    assert sanitize_agent("") == "agent"
    assert len(sanitize_agent("x" * 200)) == 64


# -- remember --------------------------------------------------------------

def test_remember_creates_then_merges_the_same_fact(svc):
    first = svc.remember("convention", CONVENTION)
    second = svc.remember("convention", CONVENTION)
    assert first["created"] is True and first["merged"] is False
    assert second["merged"] is True and second["id"] == first["id"]
    assert svc.status()["counts"]["convention"] == 1


def test_records_are_attributed_to_the_client(svc):
    svc.set_client("Cursor Editor")
    out = svc.remember("convention", CONVENTION)
    node = svc.memory.graph.get_node("convention", out["id"])
    assert node.properties["agent"] == "Cursor-Editor"


@pytest.mark.parametrize("kind", ["module", "hot_path", "snippet", "nope", ""])
def test_only_agent_kinds_are_accepted(svc, kind):
    with pytest.raises(ServiceError) as exc:
        svc.remember(kind, {"anything": "x"})
    assert code_of(exc) == "unknown_kind"


def test_every_agent_kind_can_be_written(svc):
    examples = {
        "convention": {"category": "c", "rule": "r"},
        "decision": {"title": "t", "context": "why"},
        "bug_pattern": {"name": "n", "signature": "s"},
        "alternative": {"option": "o", "rejected_because": "b"},
        "quirk": {"description": "d"},
        "workaround": {"solution": "s", "description": "d"},
        "refactoring_attempt": {"outcome": "failed", "approach": "a", "reason_failed": "r"},
        "open_question": {"question": "q"},
        "blocker": {"description": "d"},
        "partial_work": {"work_type": "w", "location": "l", "state": "s"},
    }
    assert set(examples) == set(AGENT_KINDS)
    for kind, fields in examples.items():
        assert svc.remember(kind, fields)["created"] is True


def test_missing_required_field_is_named(svc):
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", {"category": "naming"})
    assert code_of(exc) == "invalid_argument" and "rule" in exc.value.message


@pytest.mark.parametrize("fields", [None, [], "x", {}])
def test_fields_must_be_a_non_empty_object(svc, fields):
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", fields)
    assert code_of(exc) == "invalid_argument"


def test_values_must_be_strings_or_numbers(svc):
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", {**CONVENTION, "extra": ["a", "b"]})
    assert code_of(exc) == "invalid_argument"


def test_size_limits(svc):
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", {"category": "c", "rule": "x" * 2001})
    assert code_of(exc) == "too_large"
    many = {**CONVENTION, **{f"k{i}": "v" for i in range(13)}}
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", many)
    assert code_of(exc) == "too_large"
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", CONVENTION, files=["src/cache.py"] * 51)
    assert code_of(exc) == "too_large"


@pytest.mark.parametrize("name", ["agent", "status", "session_id", "created_at", "confidence",
                                  "priority", "evidence", "scope", "helpful", "id", "source",
                                  "invalid_at", "commit", "updated_at", "harmful"])
def test_fields_cannot_set_envelope_names(svc, name):
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", {**CONVENTION, name: "spoofed"})
    assert code_of(exc) == "invalid_argument" and name in exc.value.message


def test_priority_must_is_refused_unless_allowed(repo):
    default = MemoryService(resolve_config(repo=str(repo), env={}))
    with pytest.raises(ServiceError) as exc:
        default.remember("convention", CONVENTION, priority="must")
    assert code_of(exc) == "invalid_argument"
    default.close()
    allowed = MemoryService(resolve_config(repo=str(repo), allow_must=True, env={},
                                           store=str(repo / "other.memory")))
    assert allowed.remember("convention", CONVENTION, priority="must")["created"]
    allowed.close()


@pytest.mark.parametrize("confidence", [1.5, -0.1, True, "high"])
def test_confidence_must_be_a_probability(svc, confidence):
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", CONVENTION, confidence=confidence)
    assert code_of(exc) == "invalid_argument"


# -- path confinement ------------------------------------------------------

@pytest.mark.parametrize("bad", ["../outside.py", "src/../../outside.py", "", "a\x00b"])
def test_files_cannot_escape_the_repo_lexically(svc, bad):
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", CONVENTION, files=[bad])
    assert code_of(exc) in ("path_outside_repo", "invalid_argument")


def test_an_absolute_path_elsewhere_is_refused(svc, tmp_path):
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", CONVENTION, files=[str(tmp_path / "elsewhere.py")])
    assert code_of(exc) == "path_outside_repo"


def test_an_absolute_path_inside_the_repo_is_accepted_and_stored_relative(svc, repo):
    out = svc.remember("convention", CONVENTION, files=[str(repo / "src" / "cache.py")])
    assert out["created"]


def test_a_symlink_pointing_out_of_the_repo_is_refused(svc, repo, tmp_path):
    target = tmp_path / "secret.py"
    target.write_text("s = 1\n")
    link = repo / "src" / "link.py"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need a privilege on this platform")
    with pytest.raises(ServiceError) as exc:
        svc.remember("convention", CONVENTION, files=["src/link.py"])
    assert code_of(exc) == "path_outside_repo"


# -- feedback, supersede, retract, verify -----------------------------------

def test_unknown_ids_are_not_found(svc):
    for call in (lambda: svc.feedback("nope", True), lambda: svc.retract("nope", "x"),
                 lambda: svc.verify("nope"),
                 lambda: svc.supersede("nope", "convention", CONVENTION)):
        with pytest.raises(ServiceError) as exc:
            call()
        assert code_of(exc) == "not_found"


def test_feedback_counts(svc):
    rec = svc.remember("convention", CONVENTION)
    svc.feedback(rec["id"], True)
    svc.feedback(rec["id"], True)
    svc.feedback(rec["id"], False)
    props = svc.memory.graph.get_node("convention", rec["id"]).properties
    assert props["helpful"] == 2 and props["harmful"] == 1
    with pytest.raises(ServiceError):
        svc.feedback(rec["id"], "yes")


def test_supersede_replaces_and_retract_hides(svc):
    old = svc.remember("convention", CONVENTION)
    new = svc.supersede(old["id"], "convention",
                        {"category": "naming", "rule": "cache keys use dot namespaces"})
    assert new["superseded"] == old["id"] and new["id"] != old["id"]
    assert svc.status()["counts"]["convention"] == 1          # only active records count
    svc.retract(new["id"], "wrong again")
    assert svc.status()["counts"].get("convention", 0) == 0


def test_verify_restamps_anchors(svc):
    rec = svc.remember("convention", CONVENTION, files=["src/cache.py"])
    assert svc.verify(rec["id"])["restamped"] >= 1


# -- sessions --------------------------------------------------------------

def test_session_lifecycle(svc):
    started = svc.session_start("add retry logic")
    assert started["session_id"] and started["already_active"] is False
    again = svc.session_start("something else")
    assert again["already_active"] is True and again["session_id"] == started["session_id"]
    svc.remember("open_question", {"question": "cap the retries?"})
    ended = svc.session_end(summary="added a retry wrapper", completion=0.5)
    assert ended["session_id"] == started["session_id"]
    assert any("cap the retries" in i["text"] for i in ended["unresolved"])
    assert svc.status()["session"] is None
    assert svc.memory.graph.get_node("session", started["session_id"]).properties["ended"]
    texts = [n.properties.get("decision") for n in svc.memory.graph.nodes("session_decision")]
    assert "added a retry wrapper" in texts


def test_ending_without_a_session_is_an_error(svc):
    with pytest.raises(ServiceError) as exc:
        svc.session_end()
    assert code_of(exc) == "no_active_session"


def test_session_end_validates_completion(svc):
    svc.session_start("t")
    for bad in (1.5, -1, True, "done"):
        with pytest.raises(ServiceError):
            svc.session_end(completion=bad)


def test_open_items_are_carried_from_a_session_never_ended_but_not_from_an_ended_one(repo):
    config = resolve_config(repo=str(repo), env={})
    crashed = MemoryService(config)
    crashed.session_start("first task")
    crashed.remember("blocker", {"description": "CI is red on main"})
    # `crashed` never ends its session: from the store's point of view that agent died
    second = MemoryService(config)
    started = second.session_start("second task")
    assert any("CI is red" in i["text"] for i in started["carried"])
    # the engine keeps carrying an unended session's items (for up to 14 days) until it is ended
    crashed.session_end()
    second.session_end()
    third = MemoryService(config)
    assert third.session_start("third task")["carried"] == []     # everything was ended cleanly
    for s in (crashed, second, third):
        s.close()


def test_use_session_resumes_an_existing_open_session(repo):
    config = resolve_config(repo=str(repo), env={})
    a = MemoryService(config)
    sid = a.session_start("t")["session_id"]
    b = MemoryService(config)
    b.use_session(sid)
    rec = b.remember("convention", CONVENTION)
    assert b.memory.graph.get_node("convention", rec["id"]).properties["session_id"] == sid
    with pytest.raises(ServiceError) as exc:
        b.use_session("no-such-session")
    assert code_of(exc) == "not_found"
    a.close()
    b.close()


# -- status, locking, closing ----------------------------------------------

def test_status_shape(svc):
    svc.remember("convention", CONVENTION)
    status = svc.status()
    assert set(status) >= {"store", "repo_root", "agent", "shared", "session", "counts",
                           "total_active", "last_write", "unfolded_transactions", "warnings",
                           "schema"}
    assert status["shared"] is True and status["total_active"] == 1
    assert status["counts"] == {"convention": 1} and status["last_write"]


def test_calls_from_several_threads_do_not_corrupt_anything(svc):
    errors = []

    def work(n):
        try:
            for i in range(10):
                svc.remember("convention", {"category": "c", "rule": f"thread {n} rule {i}"})
        except Exception as exc:          # any exception is a failure
            errors.append(repr(exc))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and svc.status()["counts"]["convention"] == 40


def test_close_folds_the_journal(repo):
    config = resolve_config(repo=str(repo), env={})
    service = MemoryService(config)
    service.remember("convention", CONVENTION)
    assert service.memory.unfolded_transactions() > 0
    service.close()
    tail = journal.read_tail(config.store_path + ".journal", 0)
    assert tail.txs == []                                   # header only: everything is folded
    assert AgentMemory.load(config.store_path).get_stats()["total_nodes"] >= 1


def test_wrapping_an_existing_memory_does_not_close_it():
    memory = AgentMemory("t", repo_root=os.getcwd())
    service = MemoryService(memory=memory)
    assert service.remember("convention", CONVENTION)["created"]
    service.close()
    assert memory.recall(task="cache keys").items                  # still usable
