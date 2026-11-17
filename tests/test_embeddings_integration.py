import pytest

from agent_memory import AgentMemory

ison_graph_embeddings = pytest.importorskip("ison_graph_embeddings")

from agent_memory.embeddings_integration import (
    GraphEmbeddedMemory,
    create_graph_embedded_memory,
)


@pytest.fixture
def memory():
    m = AgentMemory("test-project")
    m.conventions.add_convention(
        category="naming",
        rule="use snake_case for functions",
        enforced_by="ruff",
    )
    m.conventions.add_convention(
        category="imports",
        rule="group stdlib imports first",
        enforced_by="isort",
    )
    m.decisions.add_decision(
        title="use SQLite for the embedding store",
        context="zero-dependency, good enough for single-node datasets",
    )
    return m


@pytest.fixture
def embedded(memory):
    return GraphEmbeddedMemory(memory, use_mock_encoder=True)


class TestGraphEmbeddedMemory:
    def test_requires_ison_graph_embeddings(self):
        assert GraphEmbeddedMemory is not None

    def test_index_all_counts_indexable_nodes(self, embedded):
        counts = embedded.index_all()
        assert counts["convention"] == 2
        assert counts["decision"] == 1
        assert counts["total"] == 3

    def test_index_all_is_idempotent_unless_forced(self, embedded):
        embedded.index_all()
        result = embedded.index_all()
        assert result == {"status": "already_indexed"}

        result = embedded.index_all(force=True)
        assert result["total"] == 3

    def test_search_returns_indexed_memory_types(self, embedded):
        results = embedded.search("naming conventions", k=5)
        assert len(results) > 0
        assert all(r.memory_type in ("convention", "decision") for r in results)

    def test_search_filters_by_memory_type(self, embedded):
        results = embedded.search("naming conventions", k=5, memory_types=["decision"])
        assert all(r.memory_type == "decision" for r in results)

    def test_search_indexes_lazily(self, memory):
        embedded = GraphEmbeddedMemory(memory, use_mock_encoder=True)
        assert embedded.stats()["indexed"] is False
        embedded.search("anything", k=1)
        assert embedded.stats()["indexed"] is True

    def test_find_similar_memories_excludes_self(self, embedded):
        embedded.index_all()
        node = next(n for n in embedded.memory.graph.nodes() if n.type == "convention")
        similar = embedded.find_similar_memories(node.ref, k=5)
        assert all(r.node_ref != node.ref for r in similar)

    def test_create_graph_embedded_memory_indexes_immediately(self, memory):
        embedded = create_graph_embedded_memory(memory, use_mock_encoder=True)
        assert embedded.stats()["indexed"] is True
        assert embedded.stats()["total_embeddings"] == 3

    def test_stats(self, embedded):
        embedded.index_all()
        stats = embedded.stats()
        assert stats["indexed"] is True
        assert stats["total_embeddings"] == 3


def test_indexes_registry_kinds_and_plugs_into_recall():
    m = AgentMemory("t")
    quirk = m.remember("quirk", {"description": "pipeline drops commands over the payload limit"})
    gone = m.remember("quirk", {"description": "an old belief that was wrong"})
    m.retract(gone.id, "wrong")
    embedded = GraphEmbeddedMemory(m, use_mock_encoder=True)
    counts = embedded.index_all()
    assert counts["quirk"] == 1 and counts["total"] == 1
    m.attach_semantic(embedded)
    result = m.recall(task="pipeline drops commands")   # must not raise
    assert quirk.id in repr(result)
