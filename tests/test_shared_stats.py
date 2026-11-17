from agent_memory import AgentMemory


def fill(mem):
    mem.remember("convention", {"name": "naming", "rule": "snake_case for modules"})
    mem.remember("decision", {"title": "jwt", "choice": "short-lived JWT access tokens",
                              "reason": "limits the damage of a leaked token"})
    session = mem.start_session("stats task")
    mem.todos.add_task("write the stats test")
    mem.goals.define_goal(name="consistent stats", description="one snapshot",
                          success_criteria=["numbers match"])
    mem.end_session(session)


def count_lock_acquisitions(mem):
    lock = mem._backend._lock
    calls = []
    original = lock.acquire

    def acquire(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    lock.acquire = acquire
    return calls


def test_shared_get_stats_takes_the_lock_once(tmp_path):
    mem = AgentMemory.open(str(tmp_path / "p.memory"))
    fill(mem)
    calls = count_lock_acquisitions(mem)
    mem.get_stats()
    assert len(calls) == 1


def test_shared_get_stats_equals_plain_get_stats_for_the_same_content(tmp_path):
    path = str(tmp_path / "p.memory")
    shared = AgentMemory.open(path)
    fill(shared)
    stats = shared.get_stats()
    plain = AgentMemory.load(path)              # the same content, no backend
    assert plain.is_shared is False
    assert stats == plain.get_stats()
    built = AgentMemory("p")                     # the same calls on a separate instance
    fill(built)
    assert stats == built.get_stats()


def test_shared_get_stats_sees_another_instance_write(tmp_path):
    path = str(tmp_path / "p.memory")
    a, b = AgentMemory.open(path), AgentMemory.open(path)
    before = a.get_stats()["stores"]["conventions"]
    b.remember("convention", {"name": "late", "rule": "written by the other instance"})
    after = a.get_stats()["stores"]["conventions"]
    assert after != before
