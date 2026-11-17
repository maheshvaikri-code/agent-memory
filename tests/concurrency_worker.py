"""One writer process for the concurrency tests.

Usage: python concurrency_worker.py STORE WORKER_ID N_OPS ACK_DIR
           [--compact-bytes N] [--checkpoint-every K] [--mix default|contention|reader]
           [--start-after N] [--pause SECONDS]

Appends one JSON line to ACK_DIR/ack-<WORKER_ID>.jsonl only AFTER the call it describes has
returned: an acknowledged write is one the store promised to keep. The repo root is the store's
directory, where the test puts `shared.py` for anchoring.
"""
import json
import random
import sys
import time
from pathlib import Path

from agent_memory import AgentMemory

SHARED = {"name": "shared", "rule": "every worker records this one fact"}
ANCHOR_FILE = "shared.py"
MIX = [(0.35, "add"), (0.50, "shared"), (0.65, "helpful"), (0.72, "supersede"),
       (0.80, "anchor"), (0.87, "session"), (1.01, "recall")]


def pick(roll):
    return next(op for bound, op in MIX if roll < bound)


def read_only(memory, n_reads):
    """Lockless reads while others write and compact: never an error, never a record lost."""
    seen = 0
    for _ in range(n_reads):
        memory.recall(task="worker rule")
        count = sum(1 for _ in memory.graph.nodes("convention"))
        if count < seen:
            raise SystemExit(f"a reader saw records disappear: {seen} -> {count}")
        seen = count
        time.sleep(0.005)


def wait_for_acks(ack_dir, wid, n, timeout=120.0):
    """Hold this worker back until the others have acknowledged `n` writes, so whatever it does
    next (a crash) lands while they are mid-run."""
    deadline = time.monotonic() + timeout
    while True:
        total = 0
        for path in ack_dir.glob("ack-*.jsonl"):
            if path.name != f"ack-{wid}.jsonl":
                total += path.read_text(encoding="utf-8").count("\n")
        if total >= n:
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"the other workers acknowledged only {total} of {n} writes")
        time.sleep(0.01)


def main(argv):
    store, wid, n_ops, ack_dir = argv[0], int(argv[1]), int(argv[2]), Path(argv[3])
    options = {}
    checkpoint_every = 0
    mix = "default"
    start_after = 0
    pause = 0.0
    rest = argv[4:]
    while rest:
        flag, value = rest[0], rest[1]
        if flag == "--compact-bytes":
            options["compact_min_bytes"] = int(value)
        elif flag == "--checkpoint-every":
            checkpoint_every = int(value)
        elif flag == "--mix":
            mix = value
        elif flag == "--start-after":
            start_after = int(value)
        elif flag == "--pause":
            pause = float(value)
        else:
            raise SystemExit(f"unknown flag {flag}")
        rest = rest[2:]
    memory = AgentMemory.open(store, repo_root=str(Path(store).parent), agent=f"worker-{wid}",
                              **options)
    if mix == "reader":
        read_only(memory, n_ops)
        return
    if start_after:
        wait_for_acks(ack_dir, wid, start_after)
    ack = open(ack_dir / f"ack-{wid}.jsonl", "a", buffering=1, encoding="utf-8")

    def done(**row):
        ack.write(json.dumps(row) + "\n")

    rng = random.Random(wid)
    mine = []                       # this worker's own active facts
    for i in range(n_ops):
        op = "helpful" if mix == "contention" else pick(rng.random())
        if op in ("supersede", "anchor") and not mine:
            op = "add"
        if op == "add":
            res = memory.remember("convention",
                                  {"name": f"w{wid}-{i}", "rule": f"worker {wid} rule {i}"})
            mine.append(res.id)
            done(op="add", id=res.id)
        elif op == "shared":
            res = memory.remember("convention", SHARED)
            done(op="shared", id=res.id)
        elif op == "helpful":
            res = memory.remember("convention", SHARED)
            memory.feedback(res.id, True)
            done(op="helpful", id=res.id)
        elif op == "supersede":
            old = mine.pop(rng.randrange(len(mine)))
            res = memory.supersede(old, "convention",
                                   {"name": f"w{wid}-{i}", "rule": f"worker {wid} rule {i} revised"})
            mine.append(res.id)
            done(op="supersede", id=res.id, old=old)
        elif op == "anchor":
            target = rng.choice(mine)
            memory.anchor(target, ANCHOR_FILE, f"fn_{wid}_{i}")
            done(op="anchor", id=target)
        elif op == "session":
            sid = memory.start_session(f"task {wid}-{i}")
            memory.end_session(sid, 1.0)
            done(op="session", id=sid)
        else:
            memory.recall(task=f"worker {rng.randrange(max(n_ops, 1))} rule")
        if checkpoint_every and (i + 1) % checkpoint_every == 0:
            memory.checkpoint()
        if pause:
            time.sleep(pause)            # lets the others take the lock between our writes
    memory.refresh()
    if wid % 2 == 0:
        memory.close()
    ack.close()


if __name__ == "__main__":
    main(sys.argv[1:])
