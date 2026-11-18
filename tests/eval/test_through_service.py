"""The scorecard through MemoryService (the boundary every agent transport uses) must equal the
engine-level scorecard: the untrusted-notes wrapper, the escaping and the token clamp change no
result. In-process: the stdio transport adds no logic (tests/test_mcp_*.py cover it)."""
import pytest

from agent_memory.service import MemoryService

from .eval_service import service_fns
from .harness import evaluate
from .heldout import HELDOUT
from .run import build, make_fns


@pytest.fixture(scope="module")
def cards(tmp_path_factory):
    memory = build(tmp_path_factory.mktemp("shopline-service"))
    plain = evaluate(*make_fns(memory))
    service = MemoryService(memory=memory)
    through = evaluate(*service_fns(service))
    held_plain = evaluate(*make_fns(memory), scenarios=HELDOUT)
    held_through = evaluate(*service_fns(service), scenarios=HELDOUT)
    return plain, through, held_plain, held_through


def test_the_service_boundary_does_not_change_the_scorecard(cards):
    plain, through, held_plain, held_through = cards
    assert through.recall == plain.recall and through.leaks == plain.leaks
    assert through.abstention == plain.abstention
    assert held_through.abstention == held_plain.abstention
    assert through.leaks == 0 and through.recall >= 0.90
