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


def test_the_generic_word_list_is_exactly_the_registered_one():
    registered = set("""write handle configure refactor improve export import process document set
    setup run build create remove delete implement support enable disable get show display load
    save read parse format convert move clean optimize speed""".split())   # "rename" removed: see RESULTS.md
    assert set(evidence.GENERIC_WORDS) == registered, (
        "the list changed: record each dev-split edit in tests/eval/bench/RESULTS.md and update "
        "this test with it")
    assert evidence.GENERIC_STEMS == evidence.stems(registered)


def test_the_cap_lowers_the_rare_limit_for_a_big_store(monkeypatch):
    monkeypatch.setattr(evidence, "RARE_CAP", None)
    assert c(rarest_df=14, n=100, rare_share=0.15) == MEDIUM              # limit 15
    assert c(rarest_df=14, n=100, rare_share=0.15, rare_cap=10) == WEAK   # limit min(15, 10)
    assert c(rarest_df=10, n=100, rare_share=0.15, rare_cap=10) == MEDIUM


def test_a_cap_above_the_limit_changes_nothing():
    assert c(rarest_df=5, n=100, rare_share=0.05, rare_cap=50) == MEDIUM
    assert c(rarest_df=6, n=100, rare_share=0.05, rare_cap=50) == WEAK


def test_no_cap_changes_nothing(monkeypatch):
    monkeypatch.setattr(evidence, "RARE_CAP", None)
    assert c(rarest_df=14, n=100, rare_share=0.15, rare_cap=None) == MEDIUM
    assert c(rarest_df=15, n=100, rare_share=0.15) == MEDIUM
    assert c(rarest_df=16, n=100, rare_share=0.15) == WEAK


def test_the_module_cap_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(evidence, "RARE_CAP", None)
    assert c(rarest_df=14, n=100, rare_share=0.15) == MEDIUM
    monkeypatch.setattr(evidence, "RARE_CAP", 10)
    assert c(rarest_df=14, n=100, rare_share=0.15) == WEAK
    assert c(rarest_df=14, n=100, rare_share=0.15, rare_cap=20) == MEDIUM    # explicit wins


def test_a_cap_at_or_above_distinctive_df_keeps_distinctive_words_rare():
    assert c(rarest_df=2, n=100, distinctive_df=2, rare_share=0.0, rare_cap=2) == MEDIUM
    assert c(rarest_df=3, n=100, distinctive_df=2, rare_share=0.0, rare_cap=2) == WEAK


def test_a_cap_below_distinctive_df_wins():
    """Documented behaviour: the cap is the ceiling, so a word in more than RARE_CAP records is
    not rare even if distinctive_df would allow it."""
    assert c(rarest_df=3, n=100, distinctive_df=5, rare_share=0.0) == MEDIUM
    assert c(rarest_df=3, n=100, distinctive_df=5, rare_share=0.0, rare_cap=2) == WEAK
    assert c(rarest_df=2, n=100, distinctive_df=5, rare_share=0.0, rare_cap=2) == MEDIUM


def test_the_cap_never_lowers_strong_tiers_or_identifiers():
    assert c(in_scope=True, concepts=0, rarest_df=None, rare_cap=1) == STRONG
    assert c(concepts=2, rarest_df=40, rare_cap=1) == STRONG
    assert c(rarest_df=40, n=100, rare_cap=1, identifier=True) == MEDIUM
