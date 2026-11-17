"""Non-lexical retrieval channels: file scope and one-hop graph expansion."""
from __future__ import annotations

import fnmatch
from collections import defaultdict
from typing import Dict, Iterable, List, Tuple

from ..record import MemoryRecord, NodeRef

GRAPH_EDGES = ("FIX_FOR", "WORKAROUND_FOR", "HAS_QUIRK", "CONSIDERED_FOR",
               "ATTEMPTED_ON", "INSTANCE_OF", "TEST_FOR_BUG")
GRAPH_INHERIT = 0.5


def proximity(query_file: str, anchored_file: str) -> float:
    """1.0 same file, 0.6 same directory, 0.3 shared ancestor two or more levels deep."""
    if query_file == anchored_file:
        return 1.0
    q, a = query_file.split("/")[:-1], anchored_file.split("/")[:-1]
    if q == a:
        return 0.6
    shared = 0
    for x, y in zip(q, a):
        if x != y:
            break
        shared += 1
    return 0.3 if shared >= 2 else 0.0


def scope_scores(graph, records: Dict[NodeRef, MemoryRecord],
                 files: List[str]) -> Tuple[Dict[NodeRef, float], Dict[NodeRef, str]]:
    scores: Dict[NodeRef, float] = {}
    promoted: Dict[NodeRef, str] = {}
    if not files:
        return scores, promoted

    anchored = defaultdict(list)
    for edge in graph.edges("ANCHORED_TO"):
        if edge.source in records and graph.has_node(*edge.target):
            anchored[edge.source].append(str(graph.get_node(*edge.target).properties.get("path") or ""))

    for ref, rec in records.items():
        best = 0.0
        for f in files:
            for pattern in rec.scope:
                if pattern != "*" and fnmatch.fnmatchcase(f, pattern):
                    best = 1.0
            for path in anchored.get(ref, ()):
                best = max(best, proximity(f, path))
        if best:
            scores[ref] = best

    for edge in graph.edges("CONSTRAINS"):
        if edge.source not in records or not graph.has_node(*edge.target):
            continue
        module_path = str(graph.get_node(*edge.target).properties.get("path") or "")
        module_path = module_path.replace("\\", "/").strip("/")
        if module_path and any(f.startswith(module_path) for f in files):
            scores[edge.source] = 1.0
            promoted[edge.source] = str(edge.properties.get("how") or "")
    return scores, promoted


def graph_neighbours(graph, seeds: Dict[NodeRef, float],
                     candidates: Iterable[NodeRef]) -> Dict[NodeRef, Tuple[float, NodeRef]]:
    """Candidates one whitelisted edge away from a seed, with the score they inherit and from whom."""
    allowed = set(candidates)
    out: Dict[NodeRef, Tuple[float, NodeRef]] = {}
    for rel in GRAPH_EDGES:
        for edge in graph.edges(rel):
            for seed, other in ((edge.source, edge.target), (edge.target, edge.source)):
                if seed in seeds and other in allowed and other not in seeds:
                    inherited = seeds[seed] * GRAPH_INHERIT
                    if other not in out or inherited > out[other][0]:
                        out[other] = (inherited, seed)
    return out
