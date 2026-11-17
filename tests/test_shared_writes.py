import inspect
import re
from pathlib import Path

import pytest

from agent_memory import AgentMemory
from agent_memory.stores.base import BaseStore


def two(tmp_path):
    path = str(tmp_path / "p.memory")
    return AgentMemory.open(path, agent="a"), AgentMemory.open(path, agent="b")


def active(mem, kind):
    return [n for n in mem.graph.nodes(kind) if n.properties.get("status") == "active"]


def test_two_instances_same_fact_merge_not_duplicate(tmp_path):
    a, b = two(tmp_path)
    fields = {"name": "naming", "rule": "cache keys use colon namespaces"}
    first = a.remember("convention", fields)
    second = b.remember("convention", fields)
    assert first.created and second.merged and second.id == first.id
    assert len(active(b, "convention")) == 1
    assert b.graph.get_node("convention", first.id).properties["confidence"] > 0.7


def test_remote_record_found_by_writer_index(tmp_path):
    a, b = two(tmp_path)
    b.remember("convention", {"name": "warm", "rule": "warms the writer index for this kind"})
    a.remember("convention", {"name": "late", "rule": "written by the other process"})
    result = b.remember("convention", {"name": "late", "rule": "written by the other process"})
    assert result.merged and len(active(b, "convention")) == 2


def test_writer_index_follows_a_snapshot_reload(tmp_path):
    a, b = two(tmp_path)
    b.remember("convention", {"name": "warm", "rule": "warms the writer index for this kind"})
    a.remember("convention", {"name": "late", "rule": "folded into a new snapshot"})
    a.checkpoint()          # b catches up by reloading the snapshot, not by replaying ops
    result = b.remember("convention", {"name": "late", "rule": "folded into a new snapshot"})
    assert result.merged and len(active(b, "convention")) == 2


def test_feedback_counters_are_not_lost(tmp_path):
    a, b = two(tmp_path)
    rec = a.remember("convention", {"name": "n", "rule": "count me"})
    for _ in range(10):
        a.feedback(rec.id, True)
        b.feedback(rec.id, True)
    a.refresh()
    assert a.graph.get_node("convention", rec.id).properties["helpful"] == 20


def test_supersede_across_instances(tmp_path):
    a, b = two(tmp_path)
    old = a.remember("convention", {"name": "n", "rule": "old rule"})
    new = b.supersede(old.id, "convention", {"name": "n", "rule": "new rule"})
    a.refresh()
    assert a.graph.get_node("convention", old.id).properties["status"] == "superseded"
    assert a.graph.has_edge("SUPERSEDES", new.ref, old.ref)


def test_a_session_started_by_one_agent_is_ended_by_another(tmp_path):
    a, b = two(tmp_path)
    sid = a.start_session("add retry logic")
    b.end_session(sid, completion=1.0)
    a.refresh()
    props = a.graph.get_node("session", sid).properties
    assert props.get("ended") and float(props["completion"]) == 1.0


def test_anchor_and_recall_across_instances(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "cache.py").write_text("x = 1\n")
    path = str(tmp_path / "p.memory")
    a = AgentMemory.open(path, repo_root=str(repo), agent="a")
    b = AgentMemory.open(path, repo_root=str(repo), agent="b")
    a.remember("convention", {"name": "n", "rule": "cache keys use colon namespaces"},
               anchors=["src/cache.py"])
    got = b.recall(task="cache keys", files=["src/cache.py"])
    assert got.items and got.items[0].staleness == "fresh"


def test_every_public_store_method_is_transactional():
    stores = [cls for _, cls in AgentMemory._STORES]
    assert stores
    for cls in stores:
        for name, fn in vars(cls).items():
            if name.startswith("_") or not inspect.isfunction(fn):
                continue
            if inspect.isgeneratorfunction(getattr(fn, "__wrapped__", fn)):
                continue
            assert getattr(fn, "__transactional__", False), f"{cls.__name__}.{name}"


def test_plain_stores_still_work_without_a_shared_graph():
    mem = AgentMemory("t")
    mem.decisions.add_decision("Use Postgres", "transactions")
    assert mem.decisions.get_stats()


def test_nothing_mutates_node_properties_behind_the_graphs_back():
    pattern = re.compile(r"properties\[[^\]]+\]\s*(=|\+=|-=)|properties\.(update|pop|setdefault|clear)\(")
    offenders = []
    for path in (Path(__file__).resolve().parents[1] / "src").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}")
    assert offenders == []


def test_base_store_subclass_hook_skips_generators_and_privates():
    class Demo(BaseStore):
        def get_stats(self):
            return {}

        def lines(self):
            yield 1

        def _private(self):
            return 1

    assert getattr(Demo.get_stats, "__transactional__", False)
    assert not getattr(Demo.lines, "__transactional__", False)
    assert not getattr(Demo._private, "__transactional__", False)


def test_check_suggestion_survives_a_write_landing_mid_loop(tmp_path):
    a, b = two(tmp_path)
    b.techdebt.add_debt("src/one.py", "first debt", "high")
    check = b.techdebt.check_suggestion
    landed = []

    def other_process_writes_first(location, suggestion):
        if not landed:
            landed.append(a.techdebt.add_debt("src/two.py", "second debt", "high"))
        return check(location, suggestion)       # its transaction applies a's write

    b.techdebt.check_suggestion = other_process_writes_first
    assert b.check_suggestion("rewrite it") == [] and landed


def test_saving_a_copy_includes_other_processes_writes(tmp_path):
    a, b = two(tmp_path)
    rec = a.remember("convention", {"name": "n", "rule": "written by a before b saves"})
    copy = str(tmp_path / "copy.memory")
    b.save(copy)
    assert AgentMemory.load(copy).graph.has_node("convention", rec.id)
