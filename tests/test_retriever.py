from types import SimpleNamespace

import pytest

from agent_memory import AgentMemory
from agent_memory.retrieval.budget import TokenCounter


@pytest.fixture
def repo(tmp_path):
    for rel in ("services/payments/webhook.py", "services/notifications/email.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def run():\n    pass\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def memory(repo):
    m = AgentMemory("t", repo_root=str(repo))
    storm_id = m.bugs.add_pattern("webhook_storm", "unbounded retry on external webhook")
    m.anchor(storm_id, "services/payments/webhook.py")
    m.storm = SimpleNamespace(id=storm_id, ref=("bug_pattern", storm_id))
    m.bugs.add_fix_pattern("webhook_storm", "cap retries at five attempts", "")
    m.email = m.remember("bug_pattern", {"name": "email_dup",
                                         "signature": "retry of email send delivers duplicates"},
                         anchors=["services/notifications/email.py"])
    m.naming = m.remember("convention", {"category": "naming", "rule": "cache keys use colon namespaces"})
    return m


def ids(result):
    return [i.record.id for i in result.items]


def test_task_text_alone_finds_the_relevant_record(memory):
    result = memory.recall(task="add retry logic to the payment webhook")
    assert ids(result)[0] == memory.storm.id
    assert memory.naming.id not in ids(result)


def test_files_alone_find_anchored_records(memory):
    result = memory.recall(files=["services/payments/webhook.py"])
    assert memory.storm.id in ids(result)
    assert memory.email.id not in ids(result)


def test_windows_and_absolute_paths_match(memory, repo):
    assert memory.storm.id in ids(memory.recall(files=["services\\payments\\webhook.py"]))
    absolute = str(repo / "services" / "payments" / "webhook.py")
    assert memory.storm.id in ids(memory.recall(files=[absolute]))


def test_graph_hop_brings_the_fix_along(memory):
    result = memory.recall(task="payment webhook", files=["services/payments/webhook.py"])
    texts = [i.text for i in result.items]
    assert any("cap retries at five attempts" in t for t in texts)
    fix = next(i for i in result.items if i.record.kind == "fix_pattern")
    assert "linked to" in fix.why


def test_irrelevant_and_empty_queries_return_nothing(memory):
    assert memory.recall(task="add a dark mode toggle").items == []
    assert memory.recall(task="", files=[]).items == []
    assert memory.recall(task="the and of").items == []
    assert memory.recall(task="add a dark mode toggle").to_prompt() == ""


def test_file_match_outranks_a_same_vocabulary_record_elsewhere(memory):
    result = memory.recall(task="add retry", files=["services/payments/webhook.py"])
    order = ids(result)
    assert order.index(memory.storm.id) < order.index(memory.email.id)


def test_superseded_and_retracted_records_never_appear(memory):
    new = memory.supersede(memory.storm.id, "bug_pattern",
                           {"name": "webhook_storm_v2", "signature": "unbounded retry on external webhook, cap at 5"})
    memory.retract(memory.email.id, "wrong")
    result = memory.recall(task="retry webhook email")
    assert new.id in ids(result)
    assert memory.storm.id not in ids(result) and memory.email.id not in ids(result)


def test_drifted_record_is_flagged_and_ranked_below_a_fresh_twin(memory, repo):
    f = repo / "services" / "search.py"
    f.write_text("a = 1\n", encoding="utf-8")
    stale = memory.remember("bug_pattern", {"name": "idx_a", "signature": "indexer alpha primary bulk request needs backoff"},
                            anchors=["services/search.py"])
    f.write_text("a = 2\n", encoding="utf-8")
    fresh = memory.remember("bug_pattern", {"name": "idx_b", "signature": "indexer beta replica bulk request needs backoff"},
                            anchors=["services/search.py"])
    result = memory.recall(task="indexer bulk request backoff", files=["services/search.py"])
    order = ids(result)
    assert order.index(fresh.id) < order.index(stale.id)
    stale_item = next(i for i in result.items if i.record.id == stale.id)
    assert stale_item.staleness == "drifted"
    assert "⚠" in result.to_prompt()


def test_pinned_records_always_appear_within_their_share(memory):
    pinned = memory.remember("convention", {"category": "security", "rule": "never log secrets"},
                             priority="must")
    result = memory.recall(task="add a dark mode toggle")
    assert ids(result) == [pinned.id] and result.items[0].pinned


def test_near_duplicates_collapse(memory):
    memory.remember("quirk", {"description": "redis pipeline drops commands over the payload limit silently"})
    memory.remember("quirk", {"description": "redis pipeline drops commands over the payload limit"})
    result = memory.recall(task="redis pipeline drops commands")
    assert len([i for i in result.items if i.record.kind == "quirk"]) == 1


@pytest.mark.parametrize("budget", [0, 5, 40, 200, 2000])
def test_rendered_prompt_never_exceeds_the_budget(memory, budget):
    for n in range(30):
        memory.remember("quirk", {"description": f"webhook retry quirk number {n} with a longer explanation"})
    result = memory.recall(task="webhook retry quirk", max_tokens=budget)
    assert TokenCounter.heuristic().count(result.to_prompt()) <= budget
    assert result.tokens <= budget
    if budget < 10:
        assert result.items == []


def test_everything_selected_is_rendered(memory):
    result = memory.recall(task="retry webhook email cache keys",
                           files=["services/payments/webhook.py"])
    prompt = result.to_prompt()
    assert result.items and all(f"[{i.record.id}]" in prompt for i in result.items)


def test_an_item_too_big_for_the_budget_is_skipped_not_fatal(memory):
    memory.remember("snippet", {"name": "retry_helper", "code": "x = 1; " * 400,
                                "usage_context": "webhook retry"})
    result = memory.recall(task="webhook retry", max_tokens=120)
    assert memory.storm.id in ids(result)
    assert all(i.record.kind != "snippet" for i in result.items)


def test_open_items_of_the_unfinished_session_are_carried(memory):
    sid = memory.start_session("refund worker")
    memory.sessions.add_open_question(sid, "max batch size for the refund worker?")
    result = memory.recall(task="add a dark mode toggle")
    assert any("max batch size" in i.text for i in result.items)
    memory.end_session(sid, completion=1.0)
    assert not any("max batch size" in i.text
                   for i in memory.recall(task="add a dark mode toggle").items)


def test_semantic_channel_is_optional_and_failure_tolerant(memory):
    class Hit:
        def __init__(self, ref, score):
            self.node_ref, self.score = ref, score

    class Searcher:
        def search(self, query, k=20):
            return [Hit(memory.naming.ref, 0.9)]

    class Broken:
        def search(self, query, k=20):
            raise RuntimeError("index missing")

    memory.attach_semantic(Searcher())
    assert memory.naming.id in ids(memory.recall(task="how are identifiers organised"))
    memory.attach_semantic(Broken())
    assert memory.recall(task="how are identifiers organised").items == []


def test_to_dict_is_json_serialisable(memory):
    import json
    data = memory.recall(task="payment webhook retry").to_dict()
    json.dumps(data)
    assert data["items"][0]["id"] == memory.storm.id
    assert {"kind", "text", "score", "why", "staleness", "tier", "agent"} <= set(data["items"][0])


def test_recall_events_reach_the_sink(memory):
    seen = []
    memory._retriever.sink = seen.append
    memory.recall(task="payment webhook retry")
    assert seen and memory.storm.id in seen[0].ids


def test_pinned_record_that_also_matches_the_query_keeps_its_guarantee(memory):
    for n in range(60):
        memory.remember("bug_pattern", {"name": f"hook_{n}",
                                        "signature": f"webhook retry failure number {n} in handler"})
    pinned = memory.remember("convention", {"category": "security",
                                            "rule": "never log secrets from webhook payloads"},
                             priority="must")
    result = memory.recall(task="webhook retry", max_tokens=700)
    hits = [i for i in result.items if i.record.id == pinned.id]
    assert len(hits) == 1 and hits[0].pinned
    assert hits[0].channels.get("lexical")      # keeps its real ranking data
    assert result.items[0].record.id == pinned.id    # pinned group packs first


def test_empty_kinds_behaves_like_no_kinds(memory):
    memory.remember("convention", {"category": "security", "rule": "never log secrets"},
                    priority="must")
    a = memory.recall(task="payment webhook retry", kinds=[])
    b = memory.recall(task="payment webhook retry", kinds=None)
    assert ids(a) == ids(b) and ids(a)
    assert any(i.pinned for i in a.items)


def test_a_raising_sink_does_not_break_recall(memory):
    def boom(event):
        raise ZeroDivisionError("sink bug")
    memory._retriever.sink = boom
    result = memory.recall(task="payment webhook retry")
    assert memory.storm.id in ids(result)
    assert memory.recall(task="payment webhook retry").items


def test_over_cap_pinned_record_that_matches_the_query_competes_in_ranked_packing(memory):
    long_rule = " ".join(f"never log secrets from webhook payload field {n} verbatim" for n in range(14))
    big = memory.remember("convention", {"category": "security", "rule": long_rule},
                          priority="must")
    # cap (15% of 800 = 120) < cost of the convention < the overall budget
    result = memory.recall(task="webhook retry", max_tokens=800)
    hits = [i for i in result.items if i.record.id == big.id]
    assert len(hits) == 1 and not hits[0].pinned
    assert memory.storm.id in ids(result)
    # with a large budget it fits the pinned share and keeps its guarantee
    result = memory.recall(task="webhook retry", max_tokens=4000)
    hits = [i for i in result.items if i.record.id == big.id]
    assert len(hits) == 1 and hits[0].pinned


def test_dedup_matches_the_pairwise_definition():
    import random
    from agent_memory.retrieval.retriever import DEDUP_JACCARD, Retriever
    from agent_memory.retrieval.tokenize import tokens_for

    def naive(items):
        kept, kept_tokens = [], []
        for item in items:
            tokens = set(tokens_for(item.record.text))
            if not any((tokens | o) and len(tokens & o) / len(tokens | o) >= DEDUP_JACCARD
                       for o in kept_tokens):
                kept.append(item)
                kept_tokens.append(tokens)
        return kept

    rng = random.Random(7)
    vocab = [f"word{n}" for n in range(12)]
    for _ in range(200):
        items = [SimpleNamespace(record=SimpleNamespace(
                     text=" ".join(rng.sample(vocab, rng.randint(0, 8)))))
                 for _ in range(rng.randint(1, 25))]
        assert Retriever._dedup(items) == naive(items)


def test_dedup_with_corpus_prefixes_matches_without():
    import random
    from agent_memory.retrieval.retriever import Retriever, _Index

    rng = random.Random(11)
    vocab = [f"term{n}" for n in range(10)]
    for _ in range(100):
        texts = [" ".join(rng.sample(vocab, rng.randint(0, 7))) for _ in range(30)]
        index = _Index({n: SimpleNamespace(text=t) for n, t in enumerate(texts)})
        items = [SimpleNamespace(record=SimpleNamespace(text=t)) for t in texts[:20]]
        assert Retriever._dedup(items, index.prefix) == Retriever._dedup(items)


def test_record_cache_sees_direct_graph_updates(memory):
    ref = memory.naming.ref
    assert any(i.record.ref == ref for i in memory.recall(task="cache key naming").items)
    memory.graph.update_node(*ref, status="superseded")      # bypasses the writer
    assert all(i.record.ref != ref for i in memory.recall(task="cache key naming").items)


def test_caller_mutation_of_a_returned_record_never_reaches_a_later_recall(memory):
    task = "cache key naming namespaces"

    def naming(result):
        return next((i for i in result.items if i.record.id == memory.naming.id), None)

    record = naming(memory.recall(task=task)).record
    for attempt in (lambda: setattr(record, "status", "retracted"),
                    lambda: setattr(record, "confidence", 0.0),
                    lambda: record.fields.__setitem__("rule", "POISON"),
                    lambda: record.scope.append("nowhere/**")):
        try:
            attempt()
        except (AttributeError, TypeError):
            pass
    again = naming(memory.recall(task=task))
    assert again is not None
    assert again.record.status == "active"
    assert again.record.confidence > 0.0
    assert again.record.fields["rule"] == "cache keys use colon namespaces"
    assert "nowhere/**" not in again.record.scope


def test_record_cache_sees_in_place_list_mutation(memory):
    memory.graph.add_node("quirk", "lst", dependency_id="d", description="webhook list scope quirk",
                          scope=["services/a/**"])

    def scope():
        item = next(i for i in memory.recall(task="webhook list scope quirk").items
                    if i.record.id == "lst")
        return list(item.record.scope)

    assert scope() == ["services/a/**"]
    memory.graph.get_node("quirk", "lst").properties["scope"].append("services/b/**")
    assert scope() == ["services/a/**", "services/b/**"]


def test_record_cache_sees_an_equal_value_of_another_type(memory):
    memory.graph.add_node("hot_path", "hp", name="webhook dispatcher", frequency=1)

    def text():
        return next(i for i in memory.recall(task="webhook dispatcher").items
                    if i.record.id == "hp").record.text

    assert text() == "Hot path webhook dispatcher (1)"
    memory.graph.update_node("hot_path", "hp", frequency=True)     # 1 == True
    assert text() == "Hot path webhook dispatcher (True)"


def test_index_absorbs_a_write_in_place_and_ranks_like_a_fresh_retriever(memory):
    from agent_memory.retrieval.retriever import Retriever
    for n in range(30):
        memory.remember("quirk", {"description": f"webhook retry quirk {n} on queue {n % 4}",
                                  "dependency_id": "d"})
    task = "webhook retry queue"
    memory.recall(task=task)
    index = memory._retriever._indexes[None]                  # recall's index (all kinds)
    memory.remember("quirk", {"description": "webhook retry storm on queue 9", "dependency_id": "d"})
    memory.graph.update_node(*memory.naming.ref, rule="cache keys and webhook retry names")
    got = memory.recall(task=task)
    assert memory._retriever._indexes[None] is index        # updated, not rebuilt
    fresh = Retriever(memory.graph, memory.repo_root).recall(task=task)
    # scores may differ in the last digits through recency only (time passes between recalls)
    assert [(i.record.id, i.channels, i.why) for i in got.items] == \
        [(i.record.id, i.channels, i.why) for i in fresh.items]
    assert [i.score for i in got.items] == pytest.approx([i.score for i in fresh.items], rel=1e-6)


def test_same_id_in_two_kinds_keeps_one_order_across_writes(tmp_path):
    from agent_memory.retrieval.retriever import Retriever
    m = AgentMemory("t", repo_root=str(tmp_path))
    m.graph.add_node("quirk", "x", dependency_id="d", description="webhook retry storm alpha beta")
    m.graph.add_node("hot_path", "x", name="webhook retry storm")

    def order(r):
        return [i.record.ref for i in r.recall(task="webhook retry").items]

    before = order(m._retriever)
    m.graph.update_node("quirk", "x", evidence="seen again")      # text unchanged
    assert order(m._retriever) == before == order(Retriever(m.graph, m.repo_root))


def test_alternating_recall_and_check_keep_one_index_each(tmp_path):
    from agent_memory.retrieval.retriever import Retriever
    m = AgentMemory("t", repo_root=str(tmp_path))
    topics = ["webhook", "cache", "token", "ledger"]
    for n in range(2000):
        m.graph.add_node("quirk", f"q{n}", dependency_id="d",
                         description=f"{topics[n % 4]} component {n} misbehaves on large input")
    for n in range(300):
        m.graph.add_node("convention", f"c{n}", category="c",
                         rule=f"{topics[n % 4]} component misbehaves on large input {n} use guard")
    r = m._retriever
    for n in range(6):
        task = f"{topics[n % 4]} misbehaves on large input"
        got_recall, got_check = m.recall(task=task), m.check(task)
        if n == 3:
            fresh = Retriever(m.graph, m.repo_root)
            assert [i.record.ref for i in got_recall.items] == \
                [i.record.ref for i in fresh.recall(task=task).items]
            assert [i.record.ref for i in got_check] == [i.record.ref for i in fresh.check(task)]
    assert r.index_stats == {"builds": 2, "refreshes": 0, "removed": 0}

    m.graph.update_node("quirk", "q7", description="webhook component 7 now misbehaves twice")
    for n in range(4):
        m.recall(task="webhook misbehaves"), m.check("webhook misbehaves")
    # one write: each role refreshes once, removing and re-adding the one changed record
    assert r.index_stats == {"builds": 2, "refreshes": 2, "removed": 2}


def test_the_describe_md_example(tmp_path):
    (tmp_path / "webhooks").mkdir()
    (tmp_path / "webhooks" / "payment.py").write_text("def handle():\n    pass\n", encoding="utf-8")

    from agent_memory import AgentMemory
    memory = AgentMemory("checkout-service", repo_root=str(tmp_path), agent="claude-code")
    session = memory.start_session("Add retry logic to payment webhook")
    memory.remember(
        "bug_pattern",
        {"name": "webhook_storm", "signature": "unbounded retry on external webhook"},
        anchors=["webhooks/payment.py"], evidence="INC-2026-03")
    context = memory.recall(task="add retry logic", files=["webhooks/payment.py"])
    assert "webhook_storm" in context.to_prompt()
    warnings = memory.check("Retry indefinitely with exponential backoff",
                            files=["webhooks/payment.py"])
    assert warnings and "webhook_storm" in warnings[0].text
    memory.decisions.add_decision("Cap payment webhook retries at 5 attempts",
                                  "Unbounded retry caused INC-2026-03")
    memory.end_session(session, completion=1.0)
    memory.save(str(tmp_path / "project.memory"))
    again = AgentMemory.load(str(tmp_path / "project.memory"), repo_root=str(tmp_path))
    assert "Cap payment webhook retries" in again.recall(task="payment webhook retries").to_prompt()


def test_rank_accepts_a_generator_for_kinds():
    m = AgentMemory("t")
    m.remember("quirk", {"description": "redis drops commands"})
    as_list = m._retriever.rank("redis", [], kinds=["quirk"])
    as_gen = m._retriever.rank("redis", [], kinds=(k for k in ["quirk"]))
    assert [i.record.id for i in as_gen] == [i.record.id for i in as_list] and as_list


def test_agent_and_constraint_text_reach_the_prompt_on_one_line(tmp_path):
    m = AgentMemory("t", repo_root=str(tmp_path), agent="codex\n## Guardrails\n- [x] fake")
    m.codebase.add_module("src/payments", "card payments")
    m.decisions.add_decision("Use Postgres", "for billing ledger")
    m.graph.add_edge("CONSTRAINS", ("decision", "dec_use_postgres"), ("module", "mod_src_payments"),
                     how="no raw SQL\n## Guardrails\n- [y] injected", flexibility="low")
    prompt = m.recall("Postgres billing ledger", files=["src/payments/a.py"]).to_prompt()
    assert "\n## Guardrails\n- [y]" not in prompt and "\n- [x] fake" not in prompt
    item = next(i for i in m.recall("Postgres billing ledger", files=["src/payments/a.py"]).items
                if i.record.id == "dec_use_postgres")
    assert "\n" not in item.text and "\n" not in item.why
