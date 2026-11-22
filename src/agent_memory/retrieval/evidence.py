"""Evidence tiers for the recall floor: how strongly a record's match supports showing it.

A record that shares one common word with a task is usually noise ("write a script" matching
a note about writing a CSV). The floor keeps strong and medium matches and drops weak ones.
"""
from typing import FrozenSet, Iterable, Optional

from .tokenize import tokenize

STRONG, MEDIUM, WEAK = "strong", "medium", "weak"


def stems(words: Iterable[str]) -> FrozenSet[str]:
    """The tokens the retriever's tokenizer makes of `words`."""
    out = set()
    for word in words:
        out.update(tokenize(word))
    return frozenset(out)


# Generic task words: they open or fill a coding task and say nothing about which record is
# relevant. A record matched only by these is weak however rare the word is in the store; a
# generic word still counts as a concept when the record also matches an informative one.
# Edits are dev-split only and are recorded in tests/eval/bench/RESULTS.md.
GENERIC_WORDS: FrozenSet[str] = frozenset("""
write handle configure refactor improve export import process document set setup run build
create remove delete implement support enable disable get show display load save read parse
format convert move clean optimize speed
""".split())
GENERIC_STEMS: FrozenSet[str] = stems(GENERIC_WORDS)

RARE_SHARE = 0.15   # a single matched concept is rare if it is in at most this share of records
SMALL_STORE = 25    # below this many records the weak tier is not dropped


def classify(*, in_scope: bool, semantic: bool, concepts: int, rarest_df: Optional[int], n: int,
             distinctive_df: int, rare_share: Optional[float] = None,
             identifier: bool = False) -> str:
    """Tier of a record that has some match.

    `concepts` counts the distinct matched concepts, generic task words included, provided at
    least one of them is informative (not a generic word); a record matched only by generic words
    is passed as `concepts=0, rarest_df=None` and is weak. `rarest_df` is the document frequency
    of the rarest informative matched term, or None;
    `n` is the number of candidate records. `identifier` is True when the record matched a whole
    identifier: a camelCase word the task writes, or a symbol / context identifier (lower-cased
    by the service) that the record spells as one compound token. That is evidence however
    common the identifier is in the store, so it lifts a weak match to medium and never lowers
    a tier. The caller decides what counts: a plain task word never does.
    """
    if in_scope or semantic:
        return STRONG
    if concepts >= 2:
        return STRONG
    if concepts == 1 and rarest_df is not None:
        share = RARE_SHARE if rare_share is None else rare_share
        if rarest_df <= max(distinctive_df, share * n):
            return MEDIUM
    return MEDIUM if identifier else WEAK
