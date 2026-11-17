import pytest

from agent_memory import AgentMemory


@pytest.fixture
def memory(tmp_path):
    p = tmp_path / "webhooks" / "payment.py"
    p.parent.mkdir()
    p.write_text("def handle():\n    pass\n", encoding="utf-8")
    m = AgentMemory("t", repo_root=str(tmp_path))
    m.storm = m.remember("bug_pattern", {"name": "webhook_storm",
                                         "signature": "unbounded retry on external webhook"},
                         anchors=["webhooks/payment.py"])
    m.remember("bug_pattern", {"name": "email_dup", "signature": "retry of email send duplicates"})
    m.remember("bug_pattern", {"name": "queue_dup", "signature": "retry of queue publish duplicates"})
    m.decisions.add_decision("Use Postgres for billing", "transactions")
    m.decisions.add_alternative("Use Postgres for billing", "MongoDB", "no transactions")
    m.convention = m.remember("convention", {"category": "style", "rule": "retry helpers live in utils"})
    return m


def kinds(warnings):
    return {w.record.kind for w in warnings}


def test_paraphrased_approach_with_the_file_finds_the_bug_pattern(memory):
    warnings = memory.check("Retry indefinitely with exponential backoff",
                            files=["webhooks/payment.py"])
    assert warnings[0].record.id == memory.storm.id


def test_single_common_term_without_a_file_is_not_enough(memory):
    assert memory.check("Retry indefinitely with exponential backoff") == []


def test_distinctive_single_term_finds_the_rejected_alternative(memory):
    warnings = memory.check("switch the billing ledger to MongoDB")
    assert [w.record.kind for w in warnings] == ["alternative"]


def test_only_guardrail_kinds_are_returned(memory):
    warnings = memory.check("put the retry helpers for the external webhook somewhere new",
                            files=["webhooks/payment.py"])
    assert "convention" not in kinds(warnings)


def test_unrelated_approach_returns_nothing(memory):
    assert memory.check("render the avatar as a circle") == []


def test_orphaned_records_are_excluded(memory, tmp_path):
    (tmp_path / "webhooks" / "payment.py").unlink()
    assert memory.check("unbounded retry on the external webhook") == []


def test_constraining_decision_is_checked(memory):
    memory.codebase.add_module("src/api", "HTTP layer")
    memory.decisions.add_decision("Handlers stay async", "event loop must not block")
    memory.decisions.add_constraint("Handlers stay async", "src/api", "no blocking calls in handlers")
    warnings = memory.check("add a blocking call in the async handlers", files=["src/api/orders.py"])
    assert any(w.record.kind == "decision" and "constraint:" in w.text for w in warnings)


def test_check_suggestion_keeps_its_old_shape_and_adds_guardrails(memory):
    out = memory.check_suggestion("let us use mongodb for the ledger")
    old = [w for w in out if w["type"] == "decision_conflict"]
    new = [w for w in out if w["type"] == "guardrail"]
    assert old and {"decision", "alternative", "conflict"} <= set(old[0])
    assert new and {"id", "kind", "message", "score"} <= set(new[0])
