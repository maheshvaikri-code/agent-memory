"""BM25 over short texts, in memory."""
import math
from collections import Counter
from typing import (Dict, FrozenSet, Hashable, Iterable, List, Mapping, NamedTuple, Sequence,
                    Tuple)


class Hit(NamedTuple):
    key: Hashable
    score: float
    matched: FrozenSet[str]


class BM25:
    def __init__(self, docs: Mapping[Hashable, Sequence[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf: Dict[Hashable, Counter] = {}
        self.length: Dict[Hashable, int] = {}
        self.df: Counter = Counter()
        self.postings: Dict[str, Dict[Hashable, int]] = {}     # term -> {key: tf}
        self.total = 0
        self.update((), docs)

    def update(self, remove: Iterable[Hashable], add: Mapping[Hashable, Sequence[str]]) -> None:
        """Drop the documents in `remove`, then add (or re-add) those in `add`.

        Leaves the index exactly as a fresh build over the resulting documents would
        score it; only the order of keys inside a posting dict can differ. Removing a
        document costs one dictionary deletion per distinct term.
        """
        for key in remove:
            for term in self.tf.pop(key):
                self.df[term] -= 1
                if self.df[term]:
                    del self.postings[term][key]
                else:
                    del self.df[term], self.postings[term]
            self.total -= self.length.pop(key)
        for key, tokens in add.items():
            counts = Counter(tokens)
            self.tf[key] = counts
            self.length[key] = len(tokens)
            self.total += len(tokens)
            self.df.update(counts.keys())
            for term, n in counts.items():
                self.postings.setdefault(term, {})[key] = n
        self.n = len(self.tf)
        self.avg = self.total / self.n if self.n else 0.0
        k1, b, avg = self.k1, self.b, self.avg
        self.norm = {key: (k1 * (1 - b + b * n / avg) if avg else k1)
                     for key, n in self.length.items()}

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def match(self, query_tokens: Sequence[str]
              ) -> Tuple[Dict[Hashable, float], Dict[Hashable, FrozenSet[str]]]:
        """Scores and matched terms by key, unordered. Walks the query terms' postings only."""
        terms = [t for t in dict.fromkeys(query_tokens) if t in self.postings]
        scores: Dict[Hashable, float] = {}
        masks: Dict[Hashable, int] = {}     # which terms matched, one bit per term
        for bit, term in enumerate(terms):
            idf, flag = self.idf(term), 1 << bit
            for key, tf in self.postings[term].items():
                scores[key] = scores.get(key, 0) + idf * tf * (self.k1 + 1) / (tf + self.norm[key])
                masks[key] = masks.get(key, 0) | flag
        sets: Dict[int, FrozenSet[str]] = {}
        for mask in set(masks.values()):
            sets[mask] = frozenset(t for bit, t in enumerate(terms) if mask >> bit & 1)
        return scores, {key: sets[mask] for key, mask in masks.items()}

    def search(self, query_tokens: Sequence[str]) -> List[Hit]:
        scores, matched = self.match(query_tokens)
        hits = [Hit(key, score, matched[key]) for key, score in scores.items()]
        hits.sort(key=lambda h: (-h.score, str(h.key)))
        return hits
