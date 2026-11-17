import contextlib
import datetime

import pytest
from ison_graph import DuplicateNodeError, ISONGraph

from agent_memory.storage import journal
from agent_memory.storage.shared_graph import META_ID, META_TYPE, SharedGraph


class FakeBackend:
    def __init__(self):
        self.ops, self.depth = [], 0

    @contextlib.contextmanager
    def transaction(self):
        self.depth += 1
        try:
            yield self
        finally:
            self.depth -= 1

    def record(self, text):
        assert self.depth > 0, "an op was recorded outside a transaction"
        self.ops.append(text)


def shared():
    graph = SharedGraph(name="t")
    graph._backend = FakeBackend()
    return graph


def test_inert_without_a_backend():
    graph = SharedGraph(name="t")
    graph.add_node("note", "a", text="x")
    graph.add_node("note", "b")
    graph.add_edge("LINKS", ("note", "a"), ("note", "b"))
    assert graph.has_edge("LINKS", ("note", "a"), ("note", "b"))
    with graph.transaction():
        graph.update_node("note", "a", text="y")


def test_from_ison_builds_a_shared_graph_without_recording():
    text = ISONGraph(name="x")
    text.add_node("note", "a", text="x")
    loaded = SharedGraph.from_ison(text.to_ison(), "x")
    assert isinstance(loaded, SharedGraph) and loaded._backend is None
    assert loaded.has_node("note", "a")


def test_each_mutator_records_exactly_one_op():
    graph = shared()
    backend = graph._backend
    graph.add_node("note", "a", text="x")
    graph.add_node("note", 7, text="y")
    graph.add_edge("LINKS", ("note", "a"), ("note", 7), weight=2)
    graph.update_node("note", "a", text="z")
    graph.remove_edge("LINKS", ("note", "a"), ("note", 7))
    graph.remove_node("note", 7)
    import json
    assert [json.loads(o)[0] for o in backend.ops] == ["n", "n", "e", "u", "re", "rn"]
    assert json.loads(backend.ops[1])[2] == 7           # int ids stay ints


def test_a_failing_mutation_records_nothing():
    graph = shared()
    graph.add_node("note", "a")
    before = len(graph._backend.ops)
    with pytest.raises(DuplicateNodeError):
        graph.add_node("note", "a")
    assert len(graph._backend.ops) == before


def test_unjournalable_value_fails_before_touching_the_graph():
    graph = shared()
    with pytest.raises(TypeError):
        graph.add_node("note", "a", when=datetime.datetime.now())
    assert not graph.has_node("note", "a") and graph._backend.ops == []


def test_replaying_recorded_ops_rebuilds_an_identical_graph():
    import json
    source = shared()
    source.add_node("note", "a", text="héllo", n=3, ok=True, none=None)
    source.add_node("note", 7, text="seven")
    source.add_edge("LINKS", ("note", "a"), ("note", 7), weight=2)
    source.update_node("note", "a", text="changed")
    source.add_node("note", "gone")
    source.remove_node("note", "gone")
    replica = SharedGraph(name="t")
    for text in source._backend.ops:
        replica.apply_op(json.loads(text))
    assert replica.to_ison() == source.to_ison()


def test_replaying_an_applied_op_is_idempotent_and_warns():
    graph = SharedGraph(name="t")
    warnings = []
    graph.apply_op(["n", "note", "a", {"text": "x"}], warnings)
    graph.apply_op(["n", "note", "a", {"text": "y"}], warnings)
    graph.apply_op(["rn", "note", "missing"], warnings)
    assert graph.get_node("note", "a").properties["text"] == "y" and len(warnings) == 2


def test_unknown_op_is_an_error():
    with pytest.raises(ValueError):
        SharedGraph(name="t").apply_op(["zz", "x"])


def test_remote_ops_counter_moves_on_replay_only():
    graph = shared()
    graph.add_node("note", "a")
    assert graph.remote_ops == 0
    graph.apply_op(["n", "note", "b", {}])
    assert graph.remote_ops == 1


def test_node_count_hides_the_meta_node():
    graph = SharedGraph(name="t")
    graph.add_node("note", "a")
    ISONGraph.add_node(graph, META_TYPE, META_ID, generation="g", seq=0)
    assert graph.node_count() == 1 and graph.has_node(META_TYPE, META_ID)


def test_replace_contents_swaps_everything():
    graph = SharedGraph(name="t")
    graph.add_node("note", "old")
    other = SharedGraph(name="t")
    other.add_node("note", "a", text="x")
    other.add_node("note", "b")
    other.add_edge("LINKS", ("note", "a"), ("note", "b"))
    graph._replace_contents(other)
    assert graph.to_ison() == other.to_ison() and graph.remote_ops >= 1
