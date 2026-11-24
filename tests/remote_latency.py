"""Median memory_recall latency through the in-process HTTP app. Not a test (no `test_` prefix).

    PYTHONPATH=<checkout>/src python tests/remote_latency.py

Run it against `main` (a worktree) and against this branch in the same session and compare.
"""
import statistics
import sys
import tempfile
import time
from pathlib import Path

from starlette.testclient import TestClient

from agent_memory.remote.server import build_app
from agent_memory.remote.tokens import TokenFile
from agent_memory.service import resolve_config

ACCEPT = "application/json, text/event-stream"
TOPICS = ["cache", "queue", "retry", "auth", "migration", "schema", "logging", "deploy"]


def main(calls: int = 200) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        (root / ".git").mkdir(parents=True)
        (root / "src").mkdir()
        (root / "src" / "cache.py").write_text("x = 1\n")
        base = resolve_config(repo=str(root), agent="remote", env={})
        tokens = TokenFile(Path(tmp) / "tokens.json")
        secret = tokens.create("bench", "read-write")
        app, principals = build_app(base, tokens)
        headers = {"Accept": ACCEPT, "Authorization": f"Bearer {secret}"}
        counter = [0]

        def tool(name, **arguments):
            counter[0] += 1
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": counter[0],
                                                 "method": "tools/call",
                                                 "params": {"name": name, "arguments": arguments}},
                                   headers=headers)
            response.raise_for_status()

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            for i in range(40):
                topic = TOPICS[i % len(TOPICS)]
                tool("memory_remember", kind="quirk",
                     fields={"description": f"the {topic} layer misbehaves under load case {i}"})
            for i in range(20):
                tool("memory_recall", task=f"fix the {TOPICS[i % len(TOPICS)]} problem")
            times = []
            for i in range(calls):
                started = time.perf_counter()
                tool("memory_recall", task=f"fix the {TOPICS[i % len(TOPICS)]} problem")
                times.append((time.perf_counter() - started) * 1000)
        principals.close()
    times.sort()
    print(f"median {statistics.median(times):.2f} ms  p95 {times[int(0.95 * len(times)) - 1]:.2f} ms "
          f"over {calls} calls")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200)
