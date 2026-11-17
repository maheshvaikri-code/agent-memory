"""An ISONGraph that records its mutations for the journal.

With no backend attached (`_backend is None`) it is a plain ISONGraph, which is also what it is
while a snapshot loads: `ISONGraph._build` calls `add_node`/`add_edge` on the instance.
"""
from __future__ import annotations

import contextlib
from typing import Any, Callable, List, Optional

from ison_graph import ISONGraph

from . import journal

META_TYPE = "store_meta"
META_ID = "journal"


class SharedGraph(ISONGraph):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._backend = None
        self.remote_ops = 0        # bumped whenever content arrives from outside this process

    # -- transactions ------------------------------------------------------

    def transaction(self):
        if self._backend is None:
            return contextlib.nullcontext()
        return self._backend.transaction()

    def node_count(self) -> int:
        count = super().node_count()
        return count - 1 if self.has_node(META_TYPE, META_ID) else count

    def _journaled(self, op: list, apply: Callable[[], Any]) -> Any:
        backend = self._backend
        with backend.transaction():
            text = journal.encode_op(op)        # fail before the graph is touched
            result = apply()
            backend.record(text)
            return result

    # -- mutators ----------------------------------------------------------

    def add_node(self, node_type, node_id, **properties):
        if self._backend is None:
            return ISONGraph.add_node(self, node_type, node_id, **properties)
        return self._journaled(["n", node_type, node_id, properties],
                               lambda: ISONGraph.add_node(self, node_type, node_id, **properties))

    def update_node(self, node_type, node_id, **properties):
        if self._backend is None:
            return ISONGraph.update_node(self, node_type, node_id, **properties)
        return self._journaled(["u", node_type, node_id, properties],
                               lambda: ISONGraph.update_node(self, node_type, node_id, **properties))

    def add_edge(self, rel_type, source, target, **properties):
        if self._backend is None:
            return ISONGraph.add_edge(self, rel_type, source, target, **properties)
        return self._journaled(["e", rel_type, list(source), list(target), properties],
                               lambda: ISONGraph.add_edge(self, rel_type, source, target, **properties))

    def remove_node(self, node_type, node_id):
        if self._backend is None:
            return ISONGraph.remove_node(self, node_type, node_id)
        return self._journaled(["rn", node_type, node_id],
                               lambda: ISONGraph.remove_node(self, node_type, node_id))

    def remove_edge(self, rel_type, source, target):
        if self._backend is None:
            return ISONGraph.remove_edge(self, rel_type, source, target)
        return self._journaled(["re", rel_type, list(source), list(target)],
                               lambda: ISONGraph.remove_edge(self, rel_type, source, target))

    # -- replay ------------------------------------------------------------

    def apply_op(self, op: list, warnings: Optional[List[str]] = None) -> None:
        """Apply one journaled op without recording it. Ops that are already in effect are
        applied idempotently and noted in `warnings`; they cannot arise from the protocol and
        indicate someone edited the files by hand."""
        note = warnings.append if warnings is not None else (lambda _message: None)
        code = op[0]
        self.remote_ops += 1
        if code == "n":
            _, node_type, node_id, props = op
            if self.has_node(node_type, node_id):
                note(f"journal replay: {node_type}:{node_id} already existed; updated in place")
                ISONGraph.update_node(self, node_type, node_id, **props)
            else:
                ISONGraph.add_node(self, node_type, node_id, **props)
        elif code == "u":
            _, node_type, node_id, props = op
            ISONGraph.update_node(self, node_type, node_id, **props)
        elif code == "e":
            _, rel, source, target, props = op
            source, target = tuple(source), tuple(target)
            if self.has_edge(rel, source, target):
                note(f"journal replay: edge {rel} {source}->{target} already existed")
            else:
                ISONGraph.add_edge(self, rel, source, target, **props)
        elif code == "rn":
            _, node_type, node_id = op
            if self.has_node(node_type, node_id):
                ISONGraph.remove_node(self, node_type, node_id)
            else:
                note(f"journal replay: {node_type}:{node_id} was already gone")
        elif code == "re":
            _, rel, source, target = op
            source, target = tuple(source), tuple(target)
            if self.has_edge(rel, source, target):
                ISONGraph.remove_edge(self, rel, source, target)
            else:
                note(f"journal replay: edge {rel} {source}->{target} was already gone")
        else:
            raise ValueError(f"unknown journal op {code!r}")

    def _replace_contents(self, other: ISONGraph) -> None:
        """Make this graph hold exactly what `other` holds, in place (stores, the writer and
        the retriever all keep a reference to this object)."""
        for edge in list(self.edges()):
            ISONGraph.remove_edge(self, edge.rel_type, edge.source, edge.target)
        for node in list(self.nodes()):
            ISONGraph.remove_node(self, node.type, node.id)
        for node in other.nodes():
            ISONGraph.add_node(self, node.type, node.id, **dict(node.properties))
        for edge in other.edges():
            ISONGraph.add_edge(self, edge.rel_type, edge.source, edge.target,
                               **dict(edge.properties))
        self.remote_ops += 1
