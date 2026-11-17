"""SharedGraph.node_count keeps ISONGraph's optional node_type parameter (final review C1)."""
from agent_memory import AgentMemory


def _two_conventions(memory):
    memory.remember("convention", {"name": "naming", "rule": "cache keys use colons"})
    memory.remember("convention", {"name": "errors", "rule": "raise, never return None"})


def test_node_count_by_type_on_new_loaded_and_opened_memories(tmp_path):
    plain = AgentMemory("p")
    _two_conventions(plain)
    path = str(tmp_path / "p.memory")
    plain.save(path)
    loaded = AgentMemory.load(path)
    opened = AgentMemory.open(str(tmp_path / "o.memory"))
    _two_conventions(opened)
    for memory in (plain, loaded, opened):
        assert memory.graph.node_count("convention") == 2
        assert memory.graph.node_count(node_type="convention") == 2
        assert memory.graph.node_count("decision") == 0
    assert loaded.graph.node_count() == plain.graph.node_count()
    opened.close()


def test_untyped_count_still_excludes_the_meta_node_only(tmp_path):
    opened = AgentMemory.open(str(tmp_path / "o.memory"))
    _two_conventions(opened)
    every = sum(1 for _ in opened.graph.nodes())
    assert opened.graph.node_count() == every - 1            # store_meta is not counted
    assert opened.graph.node_count("store_meta") == 1        # asked for by type: it is there
    opened.close()
