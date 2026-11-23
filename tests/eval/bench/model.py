"""Benchmark data: worlds, labelled tasks, and the checks that keep them honest."""
import json
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from agent_memory.record import KINDS
from agent_memory.retrieval.channels import GRAPH_EDGES

BENCH = Path(__file__).parent
WORLD_DIR = BENCH / "worlds"
TASK_DIR = BENCH / "tasks"
WORLDS = ("saas", "pipeline", "mobile")
SPLITS = ("dev", "test")


class BenchError(ValueError):
    """A world or task file breaks a rule of the benchmark."""


@dataclass(frozen=True)
class WorldRecord:
    key: str
    kind: str
    fields: Dict[str, object]
    scope: Optional[List[str]] = None
    pinned: bool = False
    anchors: List[List[str]] = field(default_factory=list)
    supersedes: str = ""
    retracted: str = ""
    links: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class World:
    name: str
    files: Dict[str, str]
    records: List[WorldRecord]

    def by_key(self) -> Dict[str, WorldRecord]:
        return {r.key: r for r in self.records}

    @property
    def inactive(self) -> Set[str]:
        out = {r.supersedes for r in self.records if r.supersedes}
        return out | {r.key for r in self.records if r.retracted}

    @property
    def pinned(self) -> Set[str]:
        return {r.key for r in self.records if r.pinned}


@dataclass(frozen=True)
class Task:
    world: str
    task: str
    files: List[str]
    relevant: List[str]
    acceptable: List[str]

    @property
    def empty(self) -> bool:
        return not self.relevant


def _template_fields(kind: str) -> List[str]:
    return [name for _, name, _, _ in string.Formatter().parse(KINDS[kind].template) if name]


def parse_world(name: str, data: dict) -> World:
    files = dict(data.get("files") or {})
    seen: Dict[str, int] = {}
    records: List[WorldRecord] = []
    for index, raw in enumerate(data.get("records") or []):
        key = raw.get("key")
        where = f"world {name!r} record #{index} ({key!r})"
        if not key or not isinstance(key, str):
            raise BenchError(f"{where}: a record needs a key")
        if key in seen:
            raise BenchError(f"{where}: duplicate key")
        kind = raw.get("kind")
        if kind not in KINDS:
            raise BenchError(f"{where}: unknown kind {kind!r}")
        fields = dict(raw.get("fields") or {})
        for needed in _template_fields(kind):
            if not str(fields.get(needed, "")).strip():
                raise BenchError(f"{where}: missing field {needed!r} of kind {kind!r}")
        scope_field = KINDS[kind].scope_field
        if scope_field and scope_field in fields and not isinstance(fields[scope_field], str):
            raise BenchError(f"{where}: field {scope_field!r} of kind {kind!r} must be a string "
                             "(one path or substring, or a comma-separated string), not a list; "
                             "use the record's 'scope' for a list of globs")
        rec = WorldRecord(key=key, kind=kind, fields=fields, scope=raw.get("scope"),
                          pinned=bool(raw.get("pinned")),
                          anchors=[list(a) for a in raw.get("anchors") or []],
                          supersedes=raw.get("supersedes", ""), retracted=raw.get("retracted", ""),
                          links=[dict(link) for link in raw.get("links") or []])
        if rec.supersedes and rec.supersedes not in seen:
            raise BenchError(f"{where}: supersedes an unknown record or one that is not earlier "
                             f"({rec.supersedes!r})")
        for link in rec.links:
            if link.get("rel") not in GRAPH_EDGES:
                raise BenchError(f"{where}: unknown link {link.get('rel')!r}; use one of {GRAPH_EDGES}")
            if link.get("to") not in seen:
                raise BenchError(f"{where}: link target {link.get('to')!r} must be an earlier record")
        for path, *_ in rec.anchors:
            if path not in files:
                raise BenchError(f"{where}: anchor on an unknown file {path!r}")
        seen[key] = index
        records.append(rec)
    return World(name, files, records)


def load_world(name: str) -> World:
    path = WORLD_DIR / f"{name}.json"
    return parse_world(name, json.loads(path.read_text(encoding="utf-8")))


def parse_tasks(data: dict, worlds: Dict[str, World]) -> List[Task]:
    out: List[Task] = []
    for index, raw in enumerate(data.get("tasks") or []):
        where = f"task #{index} ({raw.get('task')!r})"
        world = worlds.get(raw.get("world"))
        if world is None:
            raise BenchError(f"{where}: unknown world {raw.get('world')!r}")
        task = Task(world=world.name, task=str(raw.get("task") or ""),
                    files=list(raw.get("files") or []), relevant=list(raw.get("relevant") or []),
                    acceptable=list(raw.get("acceptable") or []))
        if not task.task.strip() and not task.files:
            raise BenchError(f"{where}: empty task (no text and no files)")
        known = world.by_key()
        for label, keys in (("relevant", task.relevant), ("acceptable", task.acceptable)):
            for key in keys:
                if key not in known:
                    raise BenchError(f"{where}: {label} names an unknown record {key!r}")
                if key in world.inactive:
                    raise BenchError(f"{where}: {label} names an inactive record {key!r}")
                if key in world.pinned:
                    raise BenchError(f"{where}: {label} names a pinned record {key!r}; "
                                     "pinned records are always allowed")
        both = set(task.relevant) & set(task.acceptable)
        if both:
            raise BenchError(f"{where}: {sorted(both)} are both relevant and acceptable")
        out.append(task)
    return out


def load_tasks(split: str) -> List[Task]:
    if split not in SPLITS:
        raise BenchError(f"unknown split {split!r}")
    worlds = {w: load_world(w) for w in WORLDS if (WORLD_DIR / f"{w}.json").exists()}
    tasks: List[Task] = []
    for name in WORLDS:
        path = TASK_DIR / f"{split}-{name}.json"
        if path.exists():
            tasks.extend(parse_tasks(json.loads(path.read_text(encoding="utf-8")), worlds))
    return tasks
