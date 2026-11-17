"""Tokeniser for memory text and task descriptions. Code-aware, no dependencies."""
import re
from functools import lru_cache
from typing import FrozenSet, List, Tuple

STOPWORDS = frozenset("""
a an and are as at be but by do does for from how i if in into is it its
no not of on or our so that the their then there this to was we what when
where which why will with you
add all any can change every fix make must need new should update use using
""".split())

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_WORD = re.compile(r"[A-Za-z0-9]+")
_VOWELS = "aeiou"


def _undouble(w: str) -> str:
    """planning -> plann -> plan. Never below four letters: "settings" must not become
    "set", nor "logging" "log" -- a short root is a different, common word."""
    if len(w) > 4 and w[-1] == w[-2] and w[-1] not in _VOWELS + "ls":
        return w[:-1]
    return w


def _stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith("ied"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes", "zes")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
        w = _undouble(w)
    elif len(w) > 4 and w.endswith("ed") and not w.endswith("eed"):
        w = w[:-2]
        w = _undouble(w)
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def tokenize(text: str) -> List[str]:
    out = []
    for word in _WORD.findall(_CAMEL.sub(r"\1 \2", text or "")):
        word = word.lower()
        if len(word) < 2 or word in STOPWORDS:
            continue
        out.append(_stem(word))
    return out


@lru_cache(maxsize=65536)
def compound_tokens(text: str) -> Tuple[str, ...]:
    """Joined form of each camelCase word in `text` ("MongoDB" -> "mongodb").

    tokenize() splits camelCase, so a query that writes the word lower-case
    ("mongodb") would never meet the stored "mongo", "db". Indexing the joined
    form on the document side closes that gap without changing tokenize().
    """
    out = []
    for word in _WORD.findall(text or ""):
        if _CAMEL.search(word):
            joined = word.lower()
            if joined not in STOPWORDS:
                out.append(_stem(joined))
    return tuple(out)


@lru_cache(maxsize=65536)
def tokens_for(text: str) -> Tuple[str, ...]:
    """Cached tokens. Unchanged records cost a dictionary lookup per recall."""
    return tuple(tokenize(text))


@lru_cache(maxsize=65536)
def token_set(text: str) -> FrozenSet[str]:
    """Cached distinct tokens, for set comparisons such as near-duplicate checks."""
    return frozenset(tokens_for(text))
