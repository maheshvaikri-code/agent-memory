from tests.eval.bench import run
from tests.eval.bench.judge import Summary


def summary(**kw):
    base = dict(tasks=100, empty_tasks=30, relevant_total=200, relevant_hits=190, noise_records=100,
                noise_tokens=4000, clean=10, leaks=0)
    base.update(kw)
    return base


def test_rules_pass_when_noise_halves_and_recall_holds():
    before = summary()
    after = summary(noise_tokens=1900, clean=26)
    by_name = {name: ok for name, ok, _ in run.rules(before, after)}
    assert all(by_name.values()), by_name


def test_rule_one_allows_a_one_point_fall_only_with_a_seventy_percent_noise_cut():
    before = summary()
    small_cut = summary(relevant_hits=188, noise_tokens=1900, clean=26)     # -1.0 pt, 52% cut
    big_cut = summary(relevant_hits=188, noise_tokens=1000, clean=26)       # -1.0 pt, 75% cut
    assert not dict((n, ok) for n, ok, _ in run.rules(before, small_cut))["recall"]
    assert dict((n, ok) for n, ok, _ in run.rules(before, big_cut))["recall"]
    too_far = summary(relevant_hits=186, noise_tokens=500, clean=26)        # -2 pt
    assert not dict((n, ok) for n, ok, _ in run.rules(before, too_far))["recall"]


def test_rules_fail_on_leaks_weak_noise_cut_and_low_clean_rate():
    before = summary()
    after = summary(leaks=1, noise_tokens=3000, clean=20)
    by_name = {name: ok for name, ok, _ in run.rules(before, after)}
    assert not by_name["leaks"] and not by_name["noise"] and not by_name["clean"]


def test_a_zero_noise_baseline_cannot_pass_the_noise_rule_by_division():
    by_name = {n: ok for n, ok, _ in run.rules(summary(noise_tokens=0), summary(noise_tokens=0))}
    assert by_name["noise"]


def test_the_dev_split_runs_end_to_end_and_reports_no_leaks(tmp_path):
    results = run.run_split("dev")
    assert results and sum(len(r.leaks) for r in results) == 0
    assert all(r.world for r in results)


def test_relabelled_heldout_counts_the_three_reviewed_tasks():
    from tests.eval.bench.heldout_relabel import ACCEPTABLE
    assert set(ACCEPTABLE) == {
        "refactor the logging setup for the CLI",
        "improve error messages in the onboarding wizard",
        "configure the sentry error reporting DSN",
    }


# --- command line guards. run_split is always stubbed: these never run a real split. ---

import json

import pytest


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The runner with a temporary runs directory and a stub in place of run_split."""
    calls = []
    monkeypatch.setattr(run, "RUNS", tmp_path)
    monkeypatch.setattr(run, "run_split", lambda split: calls.append(split) or [])
    return calls


def _saved(path, label="old"):
    path.write_text(json.dumps({"label": label, "summary": summary()}), encoding="utf-8")


def test_save_refuses_to_overwrite_a_test_split_run(cli, tmp_path, capsys):
    _saved(tmp_path / "test-after.json")
    assert run.main(["--split", "test", "--label", "after", "--save"]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert json.loads((tmp_path / "test-after.json").read_text())["label"] == "old"
    assert cli == [], "the split must not even run"


def test_save_refuses_to_overwrite_a_baseline(cli, tmp_path, capsys):
    _saved(tmp_path / "dev-baseline.json")
    assert run.main(["--split", "dev", "--label", "baseline", "--save"]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert json.loads((tmp_path / "dev-baseline.json").read_text())["label"] == "old"


def test_force_allows_overwriting_a_protected_run(cli, tmp_path):
    _saved(tmp_path / "dev-baseline.json")
    assert run.main(["--split", "dev", "--label", "baseline", "--save", "--force"]) == 0
    assert json.loads((tmp_path / "dev-baseline.json").read_text())["label"] == "baseline"


def test_save_overwrites_an_ordinary_dev_run_and_creates_a_new_protected_one(cli, tmp_path):
    _saved(tmp_path / "dev-trial.json")
    assert run.main(["--split", "dev", "--label", "trial", "--save"]) == 0
    assert run.main(["--split", "dev", "--label", "baseline", "--save"]) == 0
    assert (tmp_path / "dev-baseline.json").exists()


def test_a_missing_against_file_is_a_clean_error(cli, capsys):
    assert run.main(["--split", "dev", "--against", "nothing"]) == 2
    err = capsys.readouterr().err
    assert "no saved run to compare with" in err and "dev-nothing.json" in err
    assert cli == []


def test_against_compares_with_a_saved_run(cli, tmp_path, capsys):
    _saved(tmp_path / "dev-baseline.json")
    assert run.main(["--split", "dev", "--against", "baseline"]) == 0
    assert "rules against baseline" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--split", "dev", "--label"],
                                  ["--split", "dev", "--against"],
                                  ["--split"],
                                  ["--split", "dev", "--label", "--save"]])
def test_a_missing_option_value_is_a_clean_error(cli, capsys, argv):
    assert run.main(argv) == 2
    assert "missing value after" in capsys.readouterr().err
    assert cli == []
