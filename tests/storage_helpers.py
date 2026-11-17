import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
TESTS = str(Path(__file__).resolve().parent)


def child_env(extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([SRC, TESTS, env.get("PYTHONPATH", "")])
    env.update(extra or {})
    return env


def run_py(code, *args, env=None, timeout=60):
    return subprocess.run([sys.executable, "-c", code, *map(str, args)], capture_output=True,
                          text=True, env=child_env(env), timeout=timeout)


def spawn_py(code, *args, env=None):
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=child_env(env))
