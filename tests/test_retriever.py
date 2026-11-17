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
