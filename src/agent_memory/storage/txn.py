"""Helpers that make store code indifferent to shared vs plain mode."""
from __future__ import annotations

import contextlib
import functools


def txn(graph):
    """A transaction on `graph` when it is shared; a do-nothing context otherwise."""
    factory = getattr(graph, "transaction", None)
    return factory() if factory is not None else contextlib.nullcontext()


def transactional(fn):
    """Run a method (on an object with a `.graph`) inside one transaction: fresh state in,
    one journal line out, and no check-then-act race."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with txn(self.graph):
            return fn(self, *args, **kwargs)
    wrapper.__transactional__ = True
    return wrapper
