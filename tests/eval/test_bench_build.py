import pytest

from tests.eval.bench import judge, model
from tests.eval.bench.build import build_world

from .test_bench_core import WORLD


def _built(tmp_path):
    return build_world(model.parse_world("saas", WORLD), tmp_path)


def test_every_record_gets_an_id_and_the_map_inverts(tmp_path):
    built = _built(tmp_path)
    assert set(built.ids) == {"cap", "old", "new", "bad", "sec", "fix"}
    assert len(set(built.ids.values())) == 6
    assert {built.key_of[i]: i for i in built.key_of} == built.ids


def test_files_are_written(tmp_path):
    _built(tmp_path)
    assert (tmp_path / "svc" / "a.py").read_text() == "x = 1\n"


def test_service_prompt_shows_active_and_pinned_records_and_never_inactive_ones(tmp_path):
    built = _built(tmp_path)
    prompt = built.service.recall("cap retries storm structlog pool leak", [], max_tokens=2000)["prompt"]
    keys = {built.key_of[i] for i in judge.shown(prompt) if i in built.key_of}
    assert "cap" in keys and "sec" in keys          # relevant record and the pinned rule
    assert "new" in keys                            # the superseding rule is active
    assert not keys & built.world.inactive          # old (superseded) and bad (retracted)


def test_a_link_brings_the_fix_along(tmp_path):
    built = _built(tmp_path)
    prompt = built.service.recall("cap retries storm", [], max_tokens=2000)["prompt"]
    assert built.ids["fix"] in judge.shown(prompt)


def test_a_record_the_engine_merges_is_an_error(tmp_path):
    data = {"files": {}, "records": [
        {"key": "a", "kind": "convention",
         "fields": {"category": "x", "rule": "use the query builder", "enforced_by": "m", "exceptions": ""}},
        {"key": "b", "kind": "convention",
         "fields": {"category": "x", "rule": "use the query builder", "enforced_by": "m", "exceptions": ""}},
    ]}
    with pytest.raises(model.BenchError, match="'b'"):
        build_world(model.parse_world("saas", data), tmp_path)
