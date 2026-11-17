import threading
import time

import pytest

from agent_memory.storage.errors import StoreLockTimeout
from agent_memory.storage.lock import FileLock
from storage_helpers import run_py, spawn_py


def test_is_reentrant_within_one_instance(tmp_path):
    lock = FileLock(tmp_path / "x.lock")
    with lock:
        with lock:
            assert lock.held
    assert not lock.held


def test_second_instance_times_out_while_first_holds(tmp_path):
    first, second = FileLock(tmp_path / "x.lock"), FileLock(tmp_path / "x.lock", timeout=0.15)
    with first:
        with pytest.raises(StoreLockTimeout):
            second.acquire()
    with second:        # free again once the first lets go
        pass


def test_threads_of_one_instance_are_serialised(tmp_path):
    lock, inside, worst = FileLock(tmp_path / "x.lock"), [0], [0]

    def work():
        for _ in range(20):
            with lock:
                inside[0] += 1
                worst[0] = max(worst[0], inside[0])
                time.sleep(0.001)
                inside[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert worst[0] == 1


HOLDER = """
import sys, time
from agent_memory.storage.lock import FileLock
path, log, tag = sys.argv[1:4]
with FileLock(path, timeout=20):
    with open(log, "a") as f:
        f.write(f"{tag} start {time.monotonic():.6f}\\n")
    time.sleep(0.3)
    with open(log, "a") as f:
        f.write(f"{tag} end {time.monotonic():.6f}\\n")
"""


def test_two_processes_never_overlap(tmp_path):
    log = tmp_path / "log.txt"
    procs = [spawn_py(HOLDER, tmp_path / "x.lock", log, tag) for tag in "AB"]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
    rows = [line.split() for line in log.read_text().splitlines()]
    assert len(rows) == 4
    spans = {}
    for tag, kind, stamp in rows:
        spans.setdefault(tag, {})[kind] = float(stamp)
    first, second = sorted(spans.values(), key=lambda s: s["start"])
    assert first["end"] <= second["start"]       # monotonic clock is system-wide on both OSes


DIE_HOLDING = """
import os, sys
from agent_memory.storage.lock import FileLock
FileLock(sys.argv[1]).acquire()
os._exit(1)
"""


def test_a_dead_holder_releases_the_lock(tmp_path):
    run_py(DIE_HOLDING, tmp_path / "x.lock")
    with FileLock(tmp_path / "x.lock", timeout=3):
        pass


FAULT = """
from agent_memory.storage import faults
faults.hit("p"); print("first", flush=True)
faults.hit("p"); print("second", flush=True)
"""


def test_fault_hook_exits_on_the_nth_hit_only_when_armed(tmp_path):
    armed = run_py(FAULT, env={"AGENT_MEMORY_FAULT": "p", "AGENT_MEMORY_FAULT_AFTER": "2"})
    assert armed.returncode == 86 and "first" in armed.stdout and "second" not in armed.stdout
    other = run_py(FAULT, env={"AGENT_MEMORY_FAULT": "elsewhere"})
    assert other.returncode == 0 and "second" in other.stdout
    unarmed = run_py(FAULT)
    assert unarmed.returncode == 0
