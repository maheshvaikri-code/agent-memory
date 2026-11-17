"""Token counting for budget packing."""
import math
from typing import Callable


class TokenCounter:
    def __init__(self, fn: Callable[[str], int], name: str):
        self._fn = fn
        self.name = name

    def count(self, text: str) -> int:
        return self._fn(text) if text else 0

    @classmethod
    def heuristic(cls) -> "TokenCounter":
        """ceil(chars / 4). The default: no dependency and no network."""
        return cls(lambda text: math.ceil(len(text) / 4), "chars/4")

    @classmethod
    def tiktoken(cls, encoding: str = "cl100k_base") -> "TokenCounter":
        """Exact counts. Opt-in, because loading an encoding may download it."""
        import tiktoken
        enc = tiktoken.get_encoding(encoding)
        return cls(lambda text: len(enc.encode(text)), f"tiktoken:{encoding}")
