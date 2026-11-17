"""BM25 over short texts, in memory."""
import math
from collections import Counter
from dataclasses import dataclass
from typing import FrozenSet, Hashable, List, Mapping, Sequence


@dataclass(frozen=True)
class Hit:
    key: Hashable
    score: float
    matched: FrozenSet[str]


class BM25:
    def __init__(self, docs: Mapping[Hashable, Sequence[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = {key: Counter(tokens) for key, tokens in docs.items()}
        self.length = {key: len(tokens) for key, tokens in docs.items()}
        self.n = len(self.tf)
        self.avg = (sum(self.length.values()) / self.n) if self.n else 0.0
        self.df: Counter = Counter()
        for counts in self.tf.values():
            self.df.update(counts.keys())

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, query_tokens: Sequence[str]) -> List[Hit]:
        query = list(dict.fromkeys(query_tokens))
        hits = []
        for key, tf in self.tf.items():
            matched = [t for t in query if t in tf]
            if not matched:
                continue
            norm = self.k1 * (1 - self.b + self.b * self.length[key] / self.avg) if self.avg else self.k1
            score = sum(self.idf(t) * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in matched)
            hits.append(Hit(key, score, frozenset(matched)))
        hits.sort(key=lambda h: (-h.score, str(h.key)))
        return hits
