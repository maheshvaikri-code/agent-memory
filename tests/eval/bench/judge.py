"""Scores one task from the prompt a recall rendered. Reads only the text, like an agent would."""
from dataclasses import dataclass, field
from typing import Dict, Iterable, List

from .model import Task, World


def tokens(text: str) -> int:
    """The default token count the retriever budgets with: chars / 4, rounded up."""
    return -(-len(text) // 4)


def shown(prompt: str) -> Dict[str, str]:
    """Record id -> the rendered block (its line plus indented lines after it), in order."""
    out: Dict[str, List[str]] = {}
    current = None
    for line in prompt.splitlines():
        if line.startswith("- [") and "]" in line:
            current = line[3:line.index("]")]
            out[current] = [line]
        elif current is not None and line.startswith("  "):
            out[current].append(line)
        else:
            current = None
    return {rid: "\n".join(lines) for rid, lines in out.items()}


@dataclass
class TaskResult:
    world: str
    task: str
    empty: bool
    shown: List[str]
    relevant_total: int
    relevant_hits: int
    missed: List[str] = field(default_factory=list)
    noise: List[str] = field(default_factory=list)
    noise_tokens: int = 0
    leaks: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """An empty task that showed no noise and no leak."""
        return self.empty and not self.noise and not self.leaks


def judge(task: Task, prompt: str, key_of: Dict[str, str], world: World) -> TaskResult:
    blocks = shown(prompt)
    keys = [(key_of[rid], rid) for rid in blocks if rid in key_of]
    allowed = set(task.relevant) | set(task.acceptable) | world.pinned
    result = TaskResult(task.world, task.task, task.empty, [k for k, _ in keys],
                        len(task.relevant), 0)
    present = {k for k, _ in keys}
    result.relevant_hits = sum(1 for k in task.relevant if k in present)
    result.missed = [k for k in task.relevant if k not in present]
    for key, rid in keys:
        if key in world.inactive:
            result.leaks.append(key)
        elif key not in allowed:
            result.noise.append(key)
            result.noise_tokens += tokens(blocks[rid] + "\n")
    return result


@dataclass
class Summary:
    tasks: int = 0
    empty_tasks: int = 0
    relevant_total: int = 0
    relevant_hits: int = 0
    noise_records: int = 0
    noise_tokens: int = 0
    clean: int = 0
    leaks: int = 0

    @property
    def recall(self) -> float:
        return self.relevant_hits / self.relevant_total if self.relevant_total else 1.0

    @property
    def clean_rate(self) -> float:
        return self.clean / self.empty_tasks if self.empty_tasks else 1.0

    @property
    def noise_tokens_per_task(self) -> float:
        return self.noise_tokens / self.tasks if self.tasks else 0.0

    @property
    def noise_per_task(self) -> float:
        return self.noise_records / self.tasks if self.tasks else 0.0


def summarise(results: Iterable[TaskResult]) -> Summary:
    s = Summary()
    for r in results:
        s.tasks += 1
        s.empty_tasks += r.empty
        s.relevant_total += r.relevant_total
        s.relevant_hits += r.relevant_hits
        s.noise_records += len(r.noise)
        s.noise_tokens += r.noise_tokens
        s.clean += r.clean
        s.leaks += len(r.leaks)
    return s
