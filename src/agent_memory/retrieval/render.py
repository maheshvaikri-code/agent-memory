"""Markdown rendering. One generic path driven by each item's text, so no kind can be left out."""
from typing import Iterable, List

from ..record import BACKGROUND, GUARDRAIL, GUIDANCE, TIER_ORDER

HEADERS = {GUARDRAIL: "## Guardrails", GUIDANCE: "## Guidance", BACKGROUND: "## Background"}
STALE_FLAGS = {
    "drifted": "⚠ anchored file changed since this was recorded",
    "broken": "⚠ anchored symbol no longer found in its file",
    "orphaned": "⚠ anchored file no longer exists",
}


def render_item(item, compact: bool = False) -> str:
    lines = [f"- [{item.record.id}] {item.text}"]
    flag = STALE_FLAGS.get(item.staleness)
    if flag:
        lines.append(f"  {flag}")
    if not compact and item.why:
        lines.append(f"  why: {item.why}")
    return "\n".join(lines)


def render(items: Iterable, compact: bool = False) -> str:
    items = list(items)
    out: List[str] = []
    for tier in TIER_ORDER:
        group = [i for i in items if i.tier == tier]
        if group:
            out.append(HEADERS[tier])
            out.extend(render_item(i, compact) for i in group)
    return "\n".join(out)
