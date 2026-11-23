"""Dev-split guard for the retriever on main (the unchanged baseline). Runs the dev split only (never the locked test split).

The bounds are the baseline's dev figures (recall 0.9875, noise 1139.4 tokens per task, leaks 0),
with a 1 point recall margin and a 15% noise margin, so a later change that quietly loses recall or
adds noise shows up in the default suite.
"""
import math

import pytest

from tests.eval.bench import judge, run

DEV_RECALL = 0.9875
DEV_NOISE_TOKENS = 1139.4


@pytest.fixture(scope="module")
def dev_summary():
    return judge.summarise(run.run_split("dev"))


def test_dev_recall_does_not_fall_more_than_a_point(dev_summary):
    assert dev_summary.recall >= DEV_RECALL - 0.01, (
        f"{dev_summary.relevant_hits}/{dev_summary.relevant_total}")


def test_dev_has_no_superseded_or_retracted_leaks(dev_summary):
    assert dev_summary.leaks == 0


def test_dev_noise_tokens_per_task_stay_within_fifteen_percent(dev_summary):
    limit = math.ceil(DEV_NOISE_TOKENS * 1.15)
    assert dev_summary.noise_tokens_per_task <= limit, (
        f"{dev_summary.noise_tokens_per_task:.1f} tokens per task, limit {limit}")
