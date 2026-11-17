"""Scores an implementation from the text it renders."""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .scenarios import SCENARIOS, Scenario
from .world import MARKERS, PINNED

RecallFn = Callable[[str, List[str]], str]


@dataclass
class Scorecard:
    include_hits: int = 0
    include_total: int = 0
    leaks: int = 0
    abstain_hits: int = 0
    abstain_total: int = 0
    by_condition: Dict[str, List[bool]] = field(default_factory=lambda: defaultdict(list))
    failures: List[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.include_hits / self.include_total if self.include_total else 1.0

    @property
    def abstention(self) -> float:
        return self.abstain_hits / self.abstain_total if self.abstain_total else 1.0

    def to_markdown(self) -> str:
        rows = ["| Condition | Passed |", "|---|---|"]
        for cond, results in self.by_condition.items():
            rows.append(f"| {cond} | {sum(results)}/{len(results)} |")
        rows += ["",
                 f"- must-include recall: {self.recall:.2f} ({self.include_hits}/{self.include_total})",
                 f"- superseded or retracted leaks: {self.leaks}",
                 f"- abstention: {self.abstention:.2f} ({self.abstain_hits}/{self.abstain_total})"]
        return "\n".join(rows)


def _block(text: str, marker: str) -> str:
    """The rendered item containing `marker`: its line plus indented lines after it."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if marker in line:
            out = [line]
            for nxt in lines[i + 1:]:
                if nxt.startswith("  "):
                    out.append(nxt)
                else:
                    break
            return "\n".join(out)
    return ""


def _judge(s: Scenario, text: str, card: Scorecard) -> bool:
    ok = True
    for name in s.include:
        card.include_total += 1
        if MARKERS[name] in text:
            card.include_hits += 1
        else:
            ok = False
    for name in s.exclude:
        if MARKERS[name] in text:
            card.leaks += 1
            ok = False
    if s.empty:
        card.abstain_total += 1
        present = [n for n, mk in MARKERS.items() if mk in text and n not in PINNED]
        if present:
            ok = False
        else:
            card.abstain_hits += 1
    if s.above:
        first, second = (text.find(MARKERS[n]) for n in s.above)
        if first < 0 or (second >= 0 and second < first):
            ok = False
    for name in s.flagged:
        if "⚠" not in _block(text, MARKERS[name]):
            ok = False
    for name in s.unflagged:
        if "⚠" in _block(text, MARKERS[name]):
            ok = False
    return ok


def evaluate(recall_fn: RecallFn, check_fn: RecallFn,
             scenarios: Optional[List[Scenario]] = None) -> Scorecard:
    card = Scorecard()
    for s in (SCENARIOS if scenarios is None else scenarios):
        text = (check_fn if s.check else recall_fn)(s.task, s.files)
        ok = _judge(s, text, card)
        card.by_condition[s.condition].append(ok)
        if not ok:
            card.failures.append(f"[{s.condition}] {s.task!r} {s.files}")
    return card
