"""The held-out set relabelled after review, printed beside the original figure.

Relabelling after seeing results is a form of tuning, so the original 0.38 stays the headline.
This marks, for three tasks, a returned record as acceptable: the review of 2026-11-21 found
the logging convention a correct match for "refactor the logging setup", and the ApiError
convention arguably useful for the two error-message tasks. Nothing else is relabelled.
"""
from typing import Callable, List, Tuple

from ..heldout import HELDOUT
from ..world import MARKERS, PINNED

ACCEPTABLE = {
    "refactor the logging setup for the CLI": {"structlog"},
    "improve error messages in the onboarding wizard": {"apierror"},
    "configure the sentry error reporting DSN": {"apierror"},
}


def relabelled_abstention(recall_fn: Callable[[str, List[str]], str]) -> Tuple[int, int]:
    hits = 0
    for s in HELDOUT:
        text = recall_fn(s.task, s.files)
        allowed = PINNED | ACCEPTABLE.get(s.task, set())
        present = [n for n, marker in MARKERS.items() if marker in text and n not in allowed]
        hits += not present
    return hits, len(HELDOUT)
