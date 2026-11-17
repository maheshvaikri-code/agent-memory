import os

from agent_memory import AgentMemory

from shared_mode import force_shared


def test_force_shared_backs_plain_constructor_with_a_shared_store(tmp_path):
    original = AgentMemory.__init__
    with force_shared(tmp_path) as directory:
        mem = AgentMemory("proj", agent="a1")
        assert mem.is_shared
        mem.remember("convention", {"name": "x", "rule": "use x"})
        journals = [name for _, _, names in os.walk(directory) for name in names
                    if name.endswith(".memory.journal")]
        assert journals == ["proj.memory.journal"]
        mem.close()
    assert AgentMemory.__init__ is original


def test_force_shared_restores_init_after_an_error(tmp_path):
    original = AgentMemory.__init__
    try:
        with force_shared(tmp_path):
            raise KeyError("boom")
    except KeyError:
        pass
    assert AgentMemory.__init__ is original


def test_force_shared_keeps_the_memory_name(tmp_path):
    with force_shared(tmp_path):
        mem = AgentMemory("proj")
        assert mem.name == "proj"
        mem.close()
