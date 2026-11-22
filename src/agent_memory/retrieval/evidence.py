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
# relevant. Filled in a later step; empty means the tiers rest on document frequency alone.
GENERIC_WORDS: FrozenSet[str] = frozenset()
GENERIC_STEMS: FrozenSet[str] = stems(GENERIC_WORDS)

RARE_SHARE = 0.05   # a single matched concept is rare if it is in at most this share of records
SMALL_STORE = 0     # below this many records the weak tier is not dropped


def classify(*, in_scope: bool, semantic: bool, concepts: int, rarest_df: Optional[int], n: int,
             distinctive_df: int, rare_share: Optional[float] = None) -> str:
    """Tier of a record that has some match.

    `concepts` counts the informative matched concepts (generic words excluded);
    `rarest_df` is the document frequency of the rarest informative matched term, or None;
    `n` is the number of candidate records.
    """
    if in_scope or semantic:
        return STRONG
    if concepts >= 2:
        return STRONG
    if concepts == 1 and rarest_df is not None:
        share = RARE_SHARE if rare_share is None else rare_share
        return MEDIUM if rarest_df <= max(distinctive_df, share * n) else WEAK
    return WEAK
