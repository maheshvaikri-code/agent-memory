"""Builds a benchmark world into a real store, the way an agent would have filled it."""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from agent_memory import AgentMemory
from agent_memory.service import MemoryService

from .model import BenchError, World


@dataclass
class Built:
    world: World
    memory: AgentMemory
    service: MemoryService
    ids: Dict[str, str]
    key_of: Dict[str, str]


def build_world(world: World, root: Path) -> Built:
    root = Path(root)
    for rel, content in world.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    memory = AgentMemory(world.name, repo_root=str(root))
    ids: Dict[str, str] = {}
    refs = {}
    for rec in world.records:
        kw = {}
        if rec.scope is not None:
            kw["scope"] = list(rec.scope)
        if rec.pinned:
            kw.update(priority="must", scope=["*"])
        if rec.supersedes:
            result = memory.supersede(ids[rec.supersedes], rec.kind, dict(rec.fields), **kw)
        else:
            result = memory.remember(rec.kind, dict(rec.fields), **kw)
        if not result.created or result.merged:
            raise BenchError(f"world {world.name!r}: the engine merged record {rec.key!r} into "
                             "another record; make its text distinct")
        ids[rec.key] = result.id
        refs[rec.key] = result.ref
        for path, symbol in ((a[0], a[1] if len(a) > 1 else "") for a in rec.anchors):
            memory.anchor(result.id, path, symbol)
        for link in rec.links:
            memory.graph.add_edge(link["rel"], result.ref, refs[link["to"]])
        if rec.retracted:
            memory.retract(result.id, rec.retracted)
    return Built(world, memory, MemoryService(memory=memory), ids, {v: k for k, v in ids.items()})
