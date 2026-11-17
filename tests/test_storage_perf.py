import statistics
import subprocess
import sys
import time

import pytest

from agent_memory import AgentMemory
from storage_helpers import child_env

TIMER = """
import json, sys, time
from agent_memory import AgentMemory
mem = AgentMemory.open(sys.argv[1], agent=sys.argv[2])
lat = []
for i in range(int(sys.argv[3])):
    t = time.perf_counter()
    mem.remember("convention", {"name": f"{sys.argv[2]}-{i}", "rule": f"rule {sys.argv[2]} {i}"})
    lat.append((time.perf_counter() - t) * 1000)
print(json.dumps(lat))
"""


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def test_always_on_guard_single_writer_is_not_pathological(tmp_path):
    mem = AgentMemory.open(str(tmp_path / "p.memory"))
    best = min(_time_writes(mem, 30, tag) for tag in "abc")
    assert best < 5.0, f"30 writes took {best:.2f}s"          # generous: tens of ms each at worst


def _time_writes(mem, n, tag):
    start = time.perf_counter()
    for i in range(n):
        mem.remember("convention", {"name": f"{tag}{i}", "rule": f"guard rule {tag} {i}"})
    return time.perf_counter() - start


def test_always_on_guard_idle_refresh_is_cheap(tmp_path):
    mem = AgentMemory.open(str(tmp_path / "p.memory"))
    start = time.perf_counter()
    for _ in range(500):
        mem._sync()
    assert (time.perf_counter() - start) / 500 < 0.005


@pytest.mark.perf
def test_write_p95_under_contention(tmp_path):
    path = str(tmp_path / "p.memory")
    AgentMemory.open(path).close()
    procs = [subprocess.Popen([sys.executable, "-c", TIMER, path, f"p{i}", "60"],
                              stdout=subprocess.PIPE, text=True, env=child_env())
             for i in range(4)]
    latencies = []
    for proc in procs:
        out, _ = proc.communicate(timeout=300)
        latencies += __import__("json").loads(out)
    assert percentile(latencies, 0.95) < 15.0


@pytest.mark.perf
def test_idle_refresh_is_under_a_fifth_of_a_millisecond(tmp_path):
    mem = AgentMemory.open(str(tmp_path / "p.memory"))
    start = time.perf_counter()
    for _ in range(2000):
        mem._sync()
    assert (time.perf_counter() - start) / 2000 < 0.0002


@pytest.mark.perf
def test_open_a_5000_record_store_in_under_1_5_seconds(tmp_path):
    path = str(tmp_path / "p.memory")
    mem = AgentMemory.open(path, compact_min_bytes=1 << 40)
    with mem.graph.transaction():
        for i in range(5000):
            mem.remember("convention", {"name": f"n{i}", "rule": f"bulk rule number {i}"})
    mem.close()
    start = time.perf_counter()
    AgentMemory.open(path)
    assert time.perf_counter() - start < 1.5
