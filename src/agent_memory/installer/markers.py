"""The managed region inside a user's instruction file. Everything outside it is never touched."""
from __future__ import annotations

from . import InstallError

BEGIN_PREFIX = "<!-- agent-memory:begin"
BEGIN = "<!-- agent-memory:begin (managed by `amem install`; edits inside are overwritten) -->"
END = "<!-- agent-memory:end -->"


def has_block(text: str) -> bool:
    """True only for a valid block: exactly one BEGIN followed by exactly one END. Anything
    else (lone, reversed or duplicated markers) is not a block; `apply_block` refuses it."""
    return (text.count(BEGIN_PREFIX) == 1 and text.count(END) == 1
            and text.find(BEGIN_PREFIX) < text.find(END))


def apply_block(existing: str, body: str) -> str:
    """`existing` with the managed block set to `body`: replaced in place when present, appended
    when absent. Applying the same body twice gives the same text.

    Anything other than "no markers" or "exactly one BEGIN followed by exactly one END" is
    refused: guessing which region to replace could swallow the user's own text.
    """
    if BEGIN_PREFIX in body or END in body:
        raise InstallError("the instruction body contains an agent-memory marker")
    block = f"{BEGIN}\n{body.strip()}\n{END}"
    begins = existing.count(BEGIN_PREFIX)
    ends = existing.count(END)
    if begins == 1 and ends == 1:
        start = existing.find(BEGIN_PREFIX)
        stop = existing.find(END)
        if stop < start:
            raise InstallError("the agent-memory end marker comes before its begin marker; fix or "
                               "remove them and run the install again")
        return existing[:start] + block + existing[stop + len(END):]
    if begins or ends:
        if begins > 1 or ends > 1:
            raise InstallError("found more than one agent-memory marker; leave a single "
                               "begin/end pair (or remove them) and run the install again")
        raise InstallError("found one agent-memory marker without its pair; fix or remove it "
                           "and run the install again")
    if not existing.strip():
        return block + "\n"
    if existing.endswith("\n\n"):
        separator = ""
    elif existing.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return existing + separator + block + "\n"
