from agent_memory import AgentMemory, TokenCounter
from agent_memory.record import MemoryRecord
from agent_memory.retrieval import retriever as retriever_mod
from agent_memory.retrieval.retriever import _Index


def rec(i, text):
    return MemoryRecord(kind="convention", id=f"c{i}", fields={"rule": text}, text=text)


def cands(records):
    return {r.ref: r for r in records}


def test_prefix_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(retriever_mod, "PREFIX_CACHE_MAX", 50)
    texts = [f"distinct text number {i} with words w{i}" for i in range(400)]
    index = _Index(cands([rec(i, t) for i, t in enumerate(texts)]))
    for t in texts:                                # every text is in the corpus
        index.prefix(t)
    assert len(index._prefixes) <= 50


def test_token_order_is_rebuilt_when_dead_tokens_pile_up(monkeypatch):
    monkeypatch.setattr(retriever_mod, "ORDER_SLACK", 20)
    live = [rec(0, "alpha beta gamma")]
    index = _Index(cands(live))
    index.prefix(live[0].text)                     # builds _order
    for i in range(1, 200):                        # churn: each round replaces the record with one
        new = rec(i, f"unique{i} token{i} word{i}")  # whose tokens are all new
        index.refresh(cands([new]), limit=10)
        index.prefix(new.text)                     # always a text that is in the corpus
        assert index._order is None or len(index._order) <= len(index.bm25.df) + 20 + 10


def test_token_counter_is_public():
    mem = AgentMemory("t")
    assert mem.count_tokens("x" * 40) > 0
    mem.set_token_counter(TokenCounter(lambda text: 1000, "fixed"))
    assert mem.count_tokens("anything") == 1000
