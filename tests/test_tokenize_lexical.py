from agent_memory.retrieval.lexical import BM25
from agent_memory.retrieval.tokenize import compound_tokens, tokenize, tokens_for


def test_splits_code_identifiers_and_paths():
    assert tokenize("handleWebhook retry_delivery services/payments/webhook.py") == \
        ["handl", "webhook", "retry", "delivery", "servic", "payment", "webhook", "py"]


def test_compound_tokens_join_camel_case_words_only():
    assert compound_tokens("Rejected: MongoDB handleWebhook retry") == ("mongodb", "handlewebhook")
    assert compound_tokens("retry_delivery plain words") == ()


def test_stems_inflections_to_a_common_form():
    assert tokenize("retries retrying retried retry") == ["retry"] * 4
    assert tokenize("caching cache caches") == ["cach"] * 3
    assert tokenize("migrations migration") == ["migration"] * 2


def test_undoubling_keeps_short_roots_apart():
    assert tokenize("planned planning") == ["plan"] * 2
    assert tokenize("settings setting") == ["sett"] * 2
    assert tokenize("set") == ["set"]          # "are set immutable" is not about settings
    assert tokenize("logging logged log") == ["logg", "logg", "log"]


def test_drops_stopwords_and_single_characters():
    assert tokenize("add the a fix to we must x") == []


def test_tokens_for_is_cached_and_immutable():
    assert tokens_for("Retry storm") is tokens_for("Retry storm")
    assert isinstance(tokens_for("Retry storm"), tuple)


def test_bm25_ranks_the_doc_with_more_query_terms_first():
    docs = {
        "a": tokenize("unbounded retry on external webhook"),
        "b": tokenize("retry of email send delivers duplicates"),
        "c": tokenize("cache keys use namespaces"),
    }
    hits = BM25(docs).search(tokenize("webhook retry"))
    assert [h.key for h in hits] == ["a", "b"]
    assert hits[0].matched == frozenset({"webhook", "retry"})


def test_bm25_empty_inputs():
    assert BM25({}).search(["x"]) == []
    assert BM25({"a": ["x"]}).search([]) == []


def test_equal_docs_get_equal_scores():
    docs = {"a": ["indexer", "alpha", "bulk"], "b": ["indexer", "beta", "bulk"]}
    hits = BM25(docs).search(["indexer", "bulk"])
    assert hits[0].score == hits[1].score
