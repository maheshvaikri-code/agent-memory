import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for entry in (str(HERE), str(HERE.parent / "src")):      # src ends up first, tests/ second
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)
