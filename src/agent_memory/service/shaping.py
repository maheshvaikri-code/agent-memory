"""Turn engine objects into the JSON-able shapes agents receive."""
from __future__ import annotations

from typing import Any, Dict


def item_to_dict(item) -> Dict[str, Any]:
    record = item.record
    return {"id": str(record.id), "kind": record.kind, "tier": item.tier, "text": item.text,
            "why": item.why, "staleness": item.staleness, "agent": record.agent,
            "confidence": round(float(record.confidence), 2), "score": round(float(item.score), 4),
            "pinned": bool(item.pinned)}
