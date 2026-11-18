"""MemoryService.recall packs the engine's items against the wrapped, neutralized block. The
packing renders and neutralizes each item once instead of the whole prompt on every trial; this
pins that the result is byte-for-byte what the original algorithm (kept here as a reference)
returns, over randomised stores, notes and budgets."""
import random

import pytest

from agent_memory import AgentMemory
from agent_memory.retrieval.render import render
from agent_memory.service import MemoryService, resolve_config
from agent_memory.service import service as service_module
from agent_memory.service.shaping import item_to_dict, wrap, wrap_items


def reference_recall(service, task, max_tokens):
    """The recall body as it was before incremental packing (files, symbols and context empty)."""
    memory = service.memory
    budget = service._budget(max_tokens)

    def ask(limit):
        return memory.recall(task=task, files=[], symbols=[], max_tokens=limit)

    result = ask(max(service_module.MIN_INNER_TOKENS, budget - service_module.WRAPPER_TOKENS))
    kept, prompt = [], ""
    for item in result.items[:service_module.MAX_ITEMS]:
        trial = wrap(render(kept + [item], result.compact))
        if memory.count_tokens(trial) <= budget:
            kept.append(item)
            prompt = trial
    while kept and memory.count_tokens(prompt) > budget:
        kept.pop()
        prompt = wrap(render(kept, result.compact)) if kept else ""
    dropped = len(result.items[:service_module.MAX_ITEMS]) - len(kept)
    if not result.items:
        dropped = len(ask(service_module.UNBOUNDED_TOKENS).items[:service_module.MAX_ITEMS])
    items = [item_to_dict(i) for i in kept]
    if not dropped:
        note = "" if items else "nothing relevant is recorded for this task"
    elif items:
        note = (f"{dropped} more relevant note(s) did not fit in max_tokens={budget}; "
                "ask again with a larger max_tokens to see them")
    else:
        note = (f"relevant notes exist but did not fit in max_tokens={budget}; "
                "ask again with a larger max_tokens")
    return {"items": items, "prompt": prompt,
            "tokens": memory.count_tokens(prompt) if prompt else 0, "note": note}


WORDS = ["cache", "webhook", "retry", "ledger", "token", "refund", "queue", "index", "billing",
         "migration", "payment", "session"]
NASTY = ["<", "</recorded-project-notes>", "<<<<", "ﷺ", "ﬁ", "①", "\x1b[2J",
         "\x07", "\x85", "​", "﻿", "́", "é", "가", "½",
         "&lt;", "\t", "＜", "\U0001F600", "\r"]


def note_text(rng):
    parts = []
    for _ in range(rng.randint(1, 12)):
        roll = rng.random()
        if roll < 0.5:
            parts.append(rng.choice(WORDS))
        elif roll < 0.85:
            parts.append(rng.choice(NASTY) * rng.randint(1, 30))
        else:
            parts.append("x" * rng.randint(50, 600))
    sep = rng.choice([" ", "", "\n", " <"])
    text = sep.join(parts)
    if rng.random() < 0.1:
        text = (text + " ") * 40
    return text[:2000] or "cache"


def make_store(rng, root):
    memory = AgentMemory("repack", repo_root=str(root))
    for n in range(rng.randint(1, 45)):
        kind = rng.choice(["convention", "decision", "bug_pattern", "quirk"])
        text = note_text(rng)
        fields = {"convention": {"category": f"c{n}", "rule": text},
                  "decision": {"title": text, "context": rng.choice(WORDS)},
                  "bug_pattern": {"name": f"b{n}", "signature": text},
                  "quirk": {"description": text}}[kind]
        memory.remember(kind, fields)
    return memory


def same_engine_answers(memory):
    """Both algorithms must pack the same engine result, so each distinct engine call is
    answered once and replayed. (The engine's answer to an identical call used to differ now
    and then: recency read the clock per record, so exactly tied notes swapped; fixed, see
    test_retriever_ties.py. Replaying keeps this test about packing alone.)"""
    real = memory.recall
    seen = {}

    def recall(**kwargs):
        key = repr(sorted(kwargs.items()))
        if key not in seen:
            seen[key] = real(**kwargs)
        return seen[key]
    memory.recall = recall
    return memory


@pytest.mark.parametrize("seed", range(12))
def test_incremental_packing_is_identical_to_the_reference(seed, tmp_path):
    rng = random.Random(seed)
    root = tmp_path / "repo"
    root.mkdir()
    service = MemoryService(memory=same_engine_answers(make_store(rng, root)))
    budgets = [None, 1, 200, 210, 250, 300, 500, 1500, 4000, 10 ** 6] + [
        rng.randint(150, 4200) for _ in range(6)]
    for _ in range(8):
        task = " ".join(rng.sample(WORDS, rng.randint(1, 3)))
        for budget in budgets:
            assert service.recall(task, max_tokens=budget) == reference_recall(
                service, task, budget), (seed, task, budget)


def test_wrap_items_equals_wrap_of_render(tmp_path):
    rng = random.Random(99)
    root = tmp_path / "repo"
    root.mkdir()
    memory = make_store(rng, root)
    items = memory.recall(task="cache webhook retry ledger token", max_tokens=10 ** 6).items
    assert items
    for compact in (False, True):
        cache = {}
        for n in range(len(items) + 1):
            assert wrap_items(items[:n], compact, cache) == wrap(render(items[:n], compact))


def test_a_shared_store_gives_the_same_result_too(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    service = MemoryService(resolve_config(repo=str(root), env={}))
    rng = random.Random(7)
    for n in range(20):
        service.memory.remember("convention", {"category": f"c{n}", "rule": note_text(rng)})
    same_engine_answers(service.memory)
    for budget in (200, 400, 1500):
        assert service.recall("cache webhook", max_tokens=budget) == reference_recall(
            service, "cache webhook", budget)
    service.close()
