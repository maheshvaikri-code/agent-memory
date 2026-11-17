"""
ISONGraph Embeddings Integration for Agent Memory
==================================================

Adds semantic (embedding-based) search directly over AgentMemory's own
ISONGraph, using `ison-graph-embeddings`' `EmbeddingStore`. Unlike
`SemanticMemory` in `cache_integration.py` (which goes through
`ison-cache`'s own, separate `EmbeddingCache`), this talks to
`ison-graph-embeddings` directly - no `ison-cache` dependency at all.

Not yet published to PyPI: install locally for now with
`pip install -e <path-to-ison-graph-embeddings-checkout>`.

Usage:
    from agent_memory import AgentMemory
    from agent_memory.embeddings_integration import GraphEmbeddedMemory

    memory = AgentMemory("my-project")
    embedded = GraphEmbeddedMemory(memory)
    embedded.index_all()

    results = embedded.search("database connection pooling", k=5)
"""

from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from .memory import AgentMemory

# Try to import ison-graph-embeddings - provide fallback if not installed
try:
    from ison_graph_embeddings import EmbeddingStore, SentenceTransformerEncoder, MockEncoder
    ISON_GRAPH_EMBEDDINGS_AVAILABLE = True
except ImportError:
    ISON_GRAPH_EMBEDDINGS_AVAILABLE = False
    EmbeddingStore = None
    SentenceTransformerEncoder = None
    MockEncoder = None

from .record import KINDS, MemoryRecord


# Kept for importers. The kind registry in record.py is the source of truth;
# each kind is embedded by its rendered text.
INDEXABLE_TYPES: Dict[str, List[str]] = {kind: [] for kind in KINDS}


@dataclass
class EmbeddedResult:
    """Result from a graph-embeddings semantic search."""
    node_ref: Tuple[str, Any]
    memory_type: str
    content: str
    score: float
    properties: Dict[str, Any]


class GraphEmbeddedMemory:
    """
    Semantic search over agent memory using ison-graph-embeddings directly.

    Indexes AgentMemory's own ISONGraph nodes into an EmbeddingStore and
    searches by cosine similarity.
    """

    def __init__(
        self,
        memory: "AgentMemory",
        db_path: str = ":memory:",
        embedding_model: Optional[str] = None,
        use_mock_encoder: bool = False,
    ):
        """
        Initialize graph-embedded memory.

        Args:
            memory: AgentMemory instance to index
            db_path: SQLite path for the embedding store (":memory:" default)
            embedding_model: sentence-transformers model name (default: all-MiniLM-L6-v2)
            use_mock_encoder: use the dependency-free MockEncoder instead of
                a real sentence-transformers model (useful for tests)
        """
        if not ISON_GRAPH_EMBEDDINGS_AVAILABLE:
            raise ImportError(
                "ison-graph-embeddings not installed. Not yet published to "
                "PyPI - install it locally for now with: "
                "pip install -e <path-to-ison-graph-embeddings-checkout>"
            )

        self.memory = memory
        encoder = (
            MockEncoder()
            if use_mock_encoder
            else SentenceTransformerEncoder(model_name=embedding_model or "all-MiniLM-L6-v2")
        )
        self._store = EmbeddingStore(db_path=db_path, encoder=encoder)
        self._indexed = False

    def index_all(self, force: bool = False) -> Dict[str, int]:
        """
        Index all indexable memory nodes for semantic search.

        Args:
            force: re-index even if already indexed

        Returns:
            Dict with counts by memory type, plus "total"
        """
        if self._indexed and not force:
            return {"status": "already_indexed"}

        counts: Dict[str, int] = {}
        for kind in KINDS:
            for node in self.memory.graph.nodes(kind):
                rec = MemoryRecord.from_node(node)
                if rec.status != "active" or not rec.text:
                    continue
                self._store.add(node.ref, rec.text)
                counts[kind] = counts.get(kind, 0) + 1

        self._indexed = True
        counts["total"] = sum(counts.values())
        return counts

    def search(
        self,
        query: str,
        k: int = 5,
        memory_types: Optional[List[str]] = None,
        threshold: float = 0.0,
    ) -> List[EmbeddedResult]:
        """
        Search memories by semantic similarity.

        Args:
            query: search query
            k: number of results
            memory_types: filter by memory types
            threshold: minimum similarity score

        Returns:
            List of EmbeddedResult ordered by relevance
        """
        if not self._indexed:
            self.index_all()

        # EmbeddingStore.similarity_search only filters by a single
        # node_type; for multiple types, over-fetch and filter here.
        single_type = memory_types[0] if memory_types and len(memory_types) == 1 else None
        raw = self._store.similarity_search(
            query,
            top_k=k * 3 if memory_types and single_type is None else k,
            node_type=single_type,
            threshold=threshold,
        )

        results = []
        for r in raw:
            if memory_types and r.node_ref[0] not in memory_types:
                continue
            try:
                node = self.memory.graph.get_node_by_ref(r.node_ref)
                properties = node.properties
            except Exception:
                properties = {}
            results.append(EmbeddedResult(
                node_ref=r.node_ref,
                memory_type=r.node_ref[0],
                content=r.text,
                score=r.score,
                properties=properties,
            ))
            if len(results) >= k:
                break
        return results

    def find_similar_memories(self, node_ref: Tuple[str, Any], k: int = 5) -> List[EmbeddedResult]:
        """Find memories similar to a given memory node."""
        entry = self._store.get(node_ref)
        if entry is None:
            return []
        results = self.search(entry.text, k=k + 1)
        return [r for r in results if r.node_ref != node_ref][:k]

    def stats(self) -> Dict[str, Any]:
        """Get graph-embedded memory statistics."""
        return {
            "indexed": self._indexed,
            "total_embeddings": self._store.count(),
        }


def create_graph_embedded_memory(
    memory: "AgentMemory",
    index_immediately: bool = True,
    **options: Any,
) -> GraphEmbeddedMemory:
    """
    Create a graph-embeddings semantic search interface for AgentMemory.

    Args:
        memory: AgentMemory instance
        index_immediately: index all memories on creation
        **options: options for GraphEmbeddedMemory

    Returns:
        GraphEmbeddedMemory instance
    """
    embedded = GraphEmbeddedMemory(memory, **options)
    if index_immediately:
        embedded.index_all()
    return embedded
