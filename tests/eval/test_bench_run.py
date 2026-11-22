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
