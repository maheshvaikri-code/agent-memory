"""Shared mode refuses `.isonl` paths before touching any file (final review I3). The store
writes ISON text snapshots, so an `.isonl` path would be rewritten in the other format; plain
load()/save() of `.isonl` keep working as before."""
import os

import pytest

from agent_memory import AgentMemory
from agent_memory.storage import JournalBackend


def _plain_isonl(path, count):
    memory = AgentMemory("y")
    for i in range(count):
        memory.remember("convention", {"name": f"a{i}", "rule": f"precious isonl rule {i}"})
    memory.save(path)
    return memory


def test_open_refuses_a_new_isonl_path_and_creates_nothing(tmp_path):
    path = str(tmp_path / "x.isonl")
    with pytest.raises(ValueError, match=r"\.isonl"):
        AgentMemory.open(path)
    with pytest.raises(ValueError, match=r"\.isonl"):
        JournalBackend(path)
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize("count", [1, 2])
def test_open_leaves_an_existing_isonl_file_untouched(tmp_path, count):
    path = str(tmp_path / "y.isonl")
    _plain_isonl(path, count)
    before = open(path, "rb").read()
    with pytest.raises(ValueError):
        AgentMemory.open(path)
    assert open(path, "rb").read() == before
    assert sorted(os.listdir(tmp_path)) == ["y.isonl"]
    loaded = AgentMemory.load(path)
    assert len(list(loaded.graph.nodes("convention"))) == count


def test_plain_isonl_save_and_load_still_round_trip(tmp_path):
    path = str(tmp_path / "z.isonl")
    original = _plain_isonl(path, 2)
    loaded = AgentMemory.load(path)
    assert loaded.is_shared is False
    assert sorted(n.properties["rule"] for n in loaded.graph.nodes("convention")) == \
        sorted(n.properties["rule"] for n in original.graph.nodes("convention"))
    loaded.save(path)
    assert AgentMemory.load(path).graph.node_count("convention") == 2
