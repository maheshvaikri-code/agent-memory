"""Multi-process stress and crash injection: the acceptance gate of the shared store.

Every worker (tests/concurrency_worker.py) acknowledges a write only after the call returned;
the invariants below must hold for every acknowledged write, crash or no crash.
"""
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from ison_graph import ISONGraph

from agent_memory import AgentMemory
from agent_memory.anchors import EDGE as ANCHOR_EDGE
from agent_memory.storage import journal
from concurrency_worker import SKILL, TEMPLATE
from storage_helpers import TESTS, child_env

WORKER = str(Path(TESTS) / "concurrency_worker.py")
CRASHED = 86


def spawn(store, wid, n_ops, ack_dir, *extra, env=None):
    return subprocess.Popen([sys.executable, WORKER, str(store), str(wid), str(n_ops),
                             str(ack_dir), *map(str, extra)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=child_env(env))


def finish(procs, timeout):
    """Wait for every process; on any failure to wait, kill and reap the rest first."""
    results = []
    try:
        for proc in procs:
            _, err = proc.communicate(timeout=timeout)
            results.append((proc.returncode, err))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    return results


def new_store(tmp_path):
    store, ack = tmp_path / "p.memory", tmp_path / "ack"
    ack.mkdir()
    (tmp_path / "shared.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    return store, ack


def acked(ack_dir):
    rows = []
    for path in Path(ack_dir).glob("ack-*.jsonl"):
        rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    return rows


def state_of(graph):
    nodes = sorted((n.type, str(n.id), json.dumps(dict(n.properties), sort_keys=True, default=str))
                   for n in graph.nodes() if n.type != "store_meta")
    edges = sorted((e.rel_type, str(e.source), str(e.target),
                    json.dumps(dict(e.properties), sort_keys=True, default=str))
                   for e in graph.edges())
    return nodes, edges


def check_store(path, ack_dir, crashed=0):
    """The invariants every run must keep. `crashed` workers died with one call in flight: that
    call may or may not have reached the store, so it is the only slack allowed."""
    # the journal exactly as the workers left it: every complete line checksum-valid and in
    # sequence; only a crash may leave an unfinished last line (the next writer cuts it)
    raw = journal.read_tail(str(path) + ".journal", 0)
    assert raw.error is None and raw.generation is not None and raw.corrupt_at is None
    assert crashed or not raw.torn, "a torn journal tail without a crash"
    seqs = [tx["seq"] for tx in raw.txs]
    assert all(b == a + 1 for a, b in zip(seqs, seqs[1:])), f"journal sequence gap: {seqs}"

    mem = AgentMemory.open(str(path))
    graph = mem.graph
    rows = acked(ack_dir)
    conventions = {n.id: n.properties for n in graph.nodes("convention")}

    for row in rows:                                  # no acknowledged write lost
        if row["op"] == "session":
            assert graph.has_node("session", row["id"]), f"acknowledged session lost: {row}"
            session = graph.get_node("session", row["id"]).properties
            assert session.get("ended") and str(session.get("completion")) == "1.0", row
        elif row["op"] in ("template", "skill"):
            continue                                  # counters: checked below
        else:
            assert row["id"] in conventions, f"acknowledged write lost: {row}"
        if row["op"] == "anchor":
            assert graph.has_edge(ANCHOR_EDGE, ("convention", row["id"]), ("file", "shared.py")), \
                f"acknowledged anchor lost: {row}"
        if row["op"] == "supersede":
            assert graph.has_edge("SUPERSEDES", ("convention", row["id"]),
                                  ("convention", row["old"])), f"supersede lost: {row}"

    # superseded exactly by the acknowledged supersedes (plus one in-flight per crash)
    superseded = {cid for cid, p in conventions.items() if p.get("status") == "superseded"}
    olds = {r["old"] for r in rows if r["op"] == "supersede"}
    assert olds <= superseded, f"acknowledged supersede lost: {olds - superseded}"
    assert len(superseded - olds) <= crashed, f"unexplained supersedes: {superseded - olds}"
    assert all(p.get("status") in ("active", "superseded") for p in conventions.values())

    # the shared fact exists once, whatever its status: consolidation would hide a duplicate
    shared = [cid for cid, p in conventions.items() if p.get("name") == "shared"]
    assert len(shared) <= 1, f"the shared fact was duplicated: {shared}"
    shared_rows = [r for r in rows if r["op"] in ("shared", "helpful")]
    if shared_rows:
        assert len(shared) == 1 and {r["id"] for r in shared_rows} == set(shared)
        helpful_acks = sum(1 for r in rows if r["op"] == "helpful")
        helpful = int(conventions[shared[0]].get("helpful") or 0)
        assert helpful_acks <= helpful <= helpful_acks + crashed, \
            f"helpful={helpful}, acknowledged={helpful_acks}, crashed={crashed}"

    # counters under contention: every acknowledged increment counted exactly once
    for op, kind, field in (("template", "template", "usage_count"),
                            ("skill", "skill", "examples_seen")):
        acks = sum(1 for r in rows if r["op"] == op)
        if acks:
            [node] = [n for n in graph.nodes(kind) if n.properties.get("name") == rows_name(op)]
            total = int(node.properties.get(field) or 0)
            assert acks <= total <= acks + crashed, f"{kind} {field}={total}, acknowledged={acks}"

    names = [p.get("name") for p in conventions.values() if p.get("name") != "shared"]
    assert len(names) == len(set(names)), "a per-worker fact was duplicated"

    # compare through a text round trip, so benign type normalisation (a list stored as text)
    # cannot make an in-memory graph look different from the same graph reloaded
    before = state_of(ISONGraph.from_ison(graph.to_ison(), "x"))
    assert mem.checkpoint()
    assert state_of(ISONGraph.load(str(path))) == before, "snapshot and journal disagree"
    leftovers = [p.name for p in Path(path).parent.iterdir() if re.search(r"\.tmp\Z", p.name)]
    assert leftovers == [], f"temp snapshots left behind: {leftovers}"
    mem.close()


def rows_name(op):
    return {"template": TEMPLATE, "skill": SKILL}[op]


def seed_counters(store):
    """The template and skill the contention mix counts on, created once before the run."""
    with AgentMemory.open(str(store)) as mem:
        mem.templates.add_template(TEMPLATE, "api")
        mem.skills.add_skill(SKILL, "testing")


def run_workers(tmp_path, workers, n_ops, *extra, reads=0, timeout=300, prepare=None):
    """`workers` writers, plus one lockless reader process doing `reads` reads when > 0.
    `prepare(store)` runs before any worker starts."""
    store, ack = new_store(tmp_path)
    if prepare is not None:
        prepare(store)
    procs = [spawn(store, wid, n_ops, ack, *extra) for wid in range(workers)]
    if reads:
        procs.append(spawn(store, 99, reads, ack, "--mix", "reader"))
    for code, err in finish(procs, timeout):
        assert code == 0, err
    return store, ack


def test_processes_writing_at_once_lose_nothing(tmp_path):
    store, ack = run_workers(tmp_path, 6, 60, "--compact-bytes", 2000, reads=300)
    check_store(store, ack)
    ops = {r["op"] for r in acked(ack)}
    assert ops >= {"add", "shared", "helpful", "supersede", "anchor", "session"}


def test_the_same_fact_from_every_process_is_one_record_with_every_count(tmp_path):
    store, ack = run_workers(tmp_path, 4, 15, "--mix", "contention", "--compact-bytes", 3000,
                             prepare=seed_counters)
    check_store(store, ack)          # helpful, usage_count, examples_seen each == acks exactly
    rows = acked(ack)
    for op in ("helpful", "template", "skill"):
        assert sum(1 for r in rows if r["op"] == op) == 4 * 15, op


@pytest.mark.stress
def test_heavy_stress(tmp_path):
    store, ack = run_workers(tmp_path, 8, 200, "--compact-bytes", 20000,
                             "--checkpoint-every", 50, reads=2000, timeout=900)
    check_store(store, ack)


# how a crasher compacts: by checkpoint(), or by crossing the size threshold inside a write
BY_CHECKPOINT, BY_THRESHOLD = "checkpoint", "threshold"
COMPACTION = {BY_CHECKPOINT: ("--checkpoint-every", 5), BY_THRESHOLD: ("--compact-bytes", 1500)}


@pytest.mark.parametrize("point,after,compaction", [
    ("before_append", 7, BY_CHECKPOINT), ("mid_append", 7, BY_CHECKPOINT),
    ("after_append", 7, BY_CHECKPOINT),
    ("before_snapshot_replace", 1, BY_CHECKPOINT), ("after_snapshot_replace", 1, BY_CHECKPOINT),
    ("before_snapshot_replace", 2, BY_THRESHOLD), ("after_snapshot_replace", 2, BY_THRESHOLD)])
def test_a_process_killed_at_any_point_leaves_a_usable_store(tmp_path, point, after, compaction):
    store, ack = new_store(tmp_path)
    crasher = spawn(store, 1, 60, ack, *COMPACTION[compaction],
                    env={"AGENT_MEMORY_FAULT": point, "AGENT_MEMORY_FAULT_AFTER": str(after)})
    [(code, err)] = finish([crasher], 120)
    assert code == CRASHED, f"the fault never fired: {err}"
    assert acked(ack), "the crasher died before acknowledging anything"
    check_store(store, ack, crashed=1)
    survivor = spawn(store, 2, 30, ack, "--checkpoint-every", 7)      # the store still works
    [(code, err)] = finish([survivor], 120)
    assert code == 0, err
    check_store(store, ack, crashed=1)


@pytest.mark.parametrize("point,after,compaction", [
    ("mid_append", 9, BY_CHECKPOINT), ("after_append", 9, BY_CHECKPOINT),
    ("before_snapshot_replace", 3, BY_CHECKPOINT), ("after_snapshot_replace", 3, BY_CHECKPOINT),
    ("after_snapshot_replace", 2, BY_THRESHOLD)])
def test_a_crash_among_live_writers_does_not_hurt_them(tmp_path, point, after, compaction):
    """The survivors never compact (odd ids skip close(), no checkpoints): whatever they leave
    on disk after the crash is exactly what the protocol made of their writes, with no later
    compaction from their memory to paper over a write that went to a stale journal."""
    store, ack = new_store(tmp_path)
    procs = [spawn(store, 1, 60, ack, *COMPACTION[compaction], "--start-after", 10, "--pause", 0.05,
                   env={"AGENT_MEMORY_FAULT": point, "AGENT_MEMORY_FAULT_AFTER": str(after)})]
    procs += [spawn(store, wid, 200, ack, "--pause", 0.01) for wid in (3, 5, 7)]
    procs.append(spawn(store, 99, 300, ack, "--mix", "reader"))        # lockless reads too
    results = finish(procs, 300)
    for code, err in results:
        assert code in (0, CRASHED), err
    assert [code for code, _ in results] == [CRASHED, 0, 0, 0, 0]
    assert (ack / "ack-1.jsonl").read_text(encoding="utf-8").strip(),         "the crasher died before acknowledging anything"
    # The survivors outlived the crash and never compact, so the journal they leave must carry
    # the snapshot's generation: after a crash between snapshot replace and journal rewrite,
    # that proves a survivor followed the moved snapshot and rewrote the journal.
    meta = ISONGraph.load(str(store)).get_node("store_meta", "journal").properties
    assert journal.read_tail(str(store) + ".journal", 0).generation == str(meta["generation"]), \
        "the journal does not continue the snapshot: a survivor kept a stale journal"
    check_store(store, ack, crashed=1)


def test_readers_survive_compaction(tmp_path):
    path = str(tmp_path / "p.memory")
    writer = AgentMemory.open(path, agent="w")
    reader = AgentMemory.open(path, agent="r")
    stop, errors, counts = threading.Event(), [], []

    def read():
        try:
            while not stop.is_set():
                reader.recall(task="rule number")
                counts.append(sum(1 for _ in reader.graph.nodes("convention")))
        except Exception as exc:               # any exception here is a failure
            errors.append(repr(exc))

    thread = threading.Thread(target=read)
    thread.start()
    try:
        for i in range(80):
            writer.remember("convention", {"name": f"n{i}", "rule": f"rule number {i}"})
            if i % 5 == 0:
                writer.checkpoint()
        time.sleep(0.2)
    finally:
        stop.set()
        thread.join(timeout=60)
    assert not thread.is_alive()
    assert errors == []
    assert counts, "the reader never completed a read"
    assert any(0 < count < 80 for count in counts), "the reader never read during the writes"
    assert counts == sorted(counts), "the reader saw records disappear"
    reader.refresh()
    assert sum(1 for _ in reader.graph.nodes("convention")) == 80
