from agent_memory.retrieval import evidence
from agent_memory.retrieval.evidence import MEDIUM, STRONG, WEAK, classify
from agent_memory.retrieval.retriever import _camel_groups, _concepts
from agent_memory.retrieval.tokenize import compound_tokens, tokenize


def c(**kw):
    base = dict(in_scope=False, semantic=False, concepts=1, rarest_df=1, n=100, distinctive_df=2)
    base.update(kw)
    return classify(**base)


def test_a_scope_or_semantic_hit_is_strong():
    assert c(in_scope=True, concepts=0, rarest_df=None) == STRONG
    assert c(semantic=True, concepts=0, rarest_df=None) == STRONG


def test_two_informative_concepts_are_strong():
    assert c(concepts=2, rarest_df=40) == STRONG


def test_one_rare_concept_is_medium_and_one_common_concept_is_weak():
    assert c(rarest_df=5, n=100, rare_share=0.05) == MEDIUM          # limit max(2, 0.05 * 100) = 5
    assert c(rarest_df=6, n=100, rare_share=0.05) == WEAK


def test_no_informative_concept_is_weak():
    assert c(concepts=0, rarest_df=None) == WEAK


def test_a_small_corpus_falls_back_to_the_absolute_rule():
    assert c(rarest_df=2, n=10) == MEDIUM           # limit max(2, 0.5) = 2
    assert c(rarest_df=3, n=10) == WEAK


def test_the_share_can_be_passed_for_tuning():
    assert c(rarest_df=8, n=100, rare_share=0.10) == MEDIUM
    assert c(rarest_df=8, n=100, rare_share=0.05) == WEAK


def test_the_module_share_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(evidence, "RARE_SHARE", 0.10)
    assert c(rarest_df=8, n=100) == MEDIUM


def test_a_camelcase_word_is_one_concept():
    text = "MongoDB"
    matched = frozenset(tokenize(text) + list(compound_tokens(text)))
    assert len(matched) >= 2                          # mongo, db, mongodb
    assert _concepts(matched, _camel_groups(text)) == 1


def test_stems_uses_the_retrieval_tokenizer():
    assert evidence.stems(["write", "handle", "setting"]) == frozenset({"writ", "handl", "set"})


def test_an_identifier_lifts_a_weak_match_to_medium():
    assert c(rarest_df=40, n=40) == WEAK
    assert c(rarest_df=40, n=40, identifier=True) == MEDIUM
    assert c(concepts=0, rarest_df=None, identifier=True) == MEDIUM


def test_an_identifier_never_lowers_a_tier():
    assert c(in_scope=True, concepts=0, rarest_df=None, identifier=True) == STRONG
    assert c(concepts=2, rarest_df=40, identifier=True) == STRONG
    assert c(rarest_df=1, identifier=True) == MEDIUM
