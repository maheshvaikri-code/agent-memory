"""Store ids that embed a per-process counter stay unique across processes (final review I1).

Every process starts its class-level counters at 0, so two agents sharing one store build the
same id for their first task, goal, issue, comment or learning event. Each test resets the
counter between two instances on one store to stand in for the second process.
"""
import pytest

from agent_memory import AgentMemory
from agent_memory.stores.goals import GoalStore
from agent_memory.stores.issues import IssueStore
from agent_memory.stores.skills import SkillStore
from agent_memory.stores.todos import TodoStore


@pytest.fixture
def two_agents(tmp_path):
    path = str(tmp_path / "p.memory")
    a, b = AgentMemory.open(path, agent="a"), AgentMemory.open(path, agent="b")
    yield a, b
    a.close()
    b.close()


def _as_two_processes(monkeypatch, owner, counter, a, b, call):
    monkeypatch.setattr(owner, counter, 0)
    first = call(a)
    monkeypatch.setattr(owner, counter, 0)        # the other process: its own counter
    second = call(b)
    assert first != second
    return first, second


def test_tasks_from_two_processes(two_agents, monkeypatch):
    a, b = two_agents
    first, second = _as_two_processes(monkeypatch, TodoStore, "_task_counter", a, b,
                                      lambda m: m.todos.add_task("Fix the flaky CI test"))
    a.refresh()
    assert a.graph.has_node("task", first) and a.graph.has_node("task", second)


def test_learning_events_from_two_processes(two_agents, monkeypatch):
    a, b = two_agents
    first, second = _as_two_processes(
        monkeypatch, SkillStore, "_event_counter", a, b,
        lambda m: m.skills.record_learning_event("python", "success", "ctx"))
    a.refresh()
    assert a.graph.has_node("learning_event", first)
    assert a.graph.has_node("learning_event", second)


def test_issues_from_two_processes(two_agents, monkeypatch):
    a, b = two_agents
    first, second = _as_two_processes(
        monkeypatch, IssueStore, "_issue_counter", a, b,
        lambda m: m.issues.create_issue("Fix login redirect loop", "desc"))
    a.refresh()
    assert a.graph.has_node("issue", first) and a.graph.has_node("issue", second)


def test_comments_from_two_processes_on_one_issue(two_agents, monkeypatch):
    a, b = two_agents
    issue = a.issues.create_issue("Fix login redirect loop", "desc")
    first, second = _as_two_processes(
        monkeypatch, IssueStore, "_comment_counter", a, b,
        lambda m: m.issues.add_comment(issue, "note", "a comment"))
    a.refresh()
    assert a.graph.has_node("issue_comment", first)
    assert a.graph.has_node("issue_comment", second)


def test_comments_on_two_issues_whose_ids_share_a_prefix(monkeypatch):
    m = AgentMemory("p")
    one = m.issues.create_issue("Fix login redirect loop", "desc")
    two = m.issues.create_issue("Fix login button colour", "desc")
    assert one[:10] == two[:10]                    # comment ids embed only issue_id[:10]
    first, second = _as_two_processes(
        monkeypatch, IssueStore, "_comment_counter", m, m,
        lambda mem, ids=iter((one, two)): mem.issues.add_comment(next(ids), "note", "c"))
    assert m.graph.has_node("issue_comment", first)
    assert m.graph.has_node("issue_comment", second)


def test_goals_from_two_processes(two_agents, monkeypatch):
    a, b = two_agents
    first, second = _as_two_processes(
        monkeypatch, GoalStore, "_goal_counter", a, b,
        lambda m: m.goals.define_goal("Ship the release", "desc", ["tests pass"]))
    a.refresh()
    assert a.graph.has_node("goal", first) and a.graph.has_node("goal", second)


def test_plain_ids_are_unchanged_when_nothing_collides(monkeypatch):
    for owner, counter in ((TodoStore, "_task_counter"), (SkillStore, "_event_counter"),
                           (IssueStore, "_issue_counter"), (IssueStore, "_comment_counter"),
                           (GoalStore, "_goal_counter")):
        monkeypatch.setattr(owner, counter, 0)
    m = AgentMemory("p")
    assert m.todos.add_task("Fix the flaky CI test") == "task_fix_the_flaky_ci_tes_1"
    assert m.todos.add_task("Fix the flaky CI test") == "task_fix_the_flaky_ci_tes_2"
    assert m.skills.record_learning_event("python", "success", "ctx") == "le_python_1"
    issue = m.issues.create_issue("Fix login redirect loop", "desc")
    assert issue == "issue_fix_login_redirect_l_1"
    assert m.issues.add_comment(issue, "note", "c") == "comment_issue_fix__1"
    assert m.goals.define_goal("Ship the release", "d", ["ok"]) == "goal_ship_the_release_1"
