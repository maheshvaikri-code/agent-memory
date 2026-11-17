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


def test_short_roots_collapse_but_plural_ing_nouns_keep_their_ing():
    assert tokenize("planned planning plan") == ["plan"] * 3
    assert tokenize("logging logged log") == ["log"] * 3
    assert tokenize("mapping mapped map") == ["map"] * 3
    assert tokenize("running run") == ["run"] * 2
    assert tokenize("getting get") == ["get"] * 2
    assert tokenize("tagged tag") == ["tag"] * 2
    # "settings" is a noun: it must not meet the verb in "are set immutable"
    assert tokenize("settings") == ["setting"]
    assert tokenize("set") == ["set"]
    # a plural -ings word on a longer stem still collapses with its singular
    for plural, singular, root in (("warnings", "warning", "warn"), ("bindings", "binding", "bind"),
                                   ("findings", "finding", "find"), ("headings", "heading", "head"),
                                   ("embeddings", "embedding", "embed")):
        assert tokenize(f"{plural} {singular}") == [root, root]


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


def test_bm25_update_scores_like_a_fresh_build():
    import random
    rng = random.Random(3)
    vocab = [f"w{n}" for n in range(15)]
    docs = {n: [rng.choice(vocab) for _ in range(rng.randint(1, 8))] for n in range(40)}
    bm25 = BM25(docs)
    for _ in range(30):
        remove = rng.sample(sorted(docs), 3)
        for key in remove:
            del docs[key]
        add = {rng.randint(0, 60): [rng.choice(vocab) for _ in range(rng.randint(1, 8))]
               for _ in range(4)}
        bm25.update([k for k in remove] + [k for k in add if k in docs], add)
        docs.update(add)
        fresh = BM25(docs)
        query = rng.sample(vocab, 4)
        assert bm25.match(query) == fresh.match(query)
        assert bm25.df == fresh.df and bm25.norm == fresh.norm and bm25.n == fresh.n
