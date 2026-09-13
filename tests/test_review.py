"""The pre-registered kill / shrink decision (design §7.6, plan §P6).

The point of pre-registration is that the decision to stop is *computed* from
the journal, not made in a drawdown. These tests pin the criteria in order and
prove that noise cannot kill a sleeve.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from signald.lineage import (
    KEEP,
    KILL,
    MIN_TRADES_FOR_VERDICT,
    SHRINK,
    kill_shrink_decision,
    write_review_report,
)

pytestmark = pytest.mark.timeout(120)


def decision(**kw):
    base = {
        "sleeve": "intraday",
        "trades": 150,
        "expectancy_usd": 4.0,
        "current_pct": 0.30,
        "sample_adequate": True,
    }
    base.update(kw)
    return kill_shrink_decision(**base)


# --- criterion 1: a negative net expectation -------------------------------
def test_a_negative_expectation_over_an_adequate_sample_kills():
    d = decision(expectancy_usd=-3.5)

    assert d.verdict == KILL and d.reason_code == "negative_expectation"
    assert d.acts and d.target_pct is None
    assert "150 trades" in d.detail


def test_the_interval_must_also_exclude_the_backtest_prediction():
    """A negative live result that is still inside the predicted interval waits."""
    interval = {"ci_low": -5.0, "ci_high": 6.0, "excludes_zero": False}
    inside = decision(expectancy_usd=-1.0, predicted_expectancy_usd=2.0, bootstrap=interval)
    assert inside.verdict != KILL

    excluded = decision(
        expectancy_usd=-1.0,
        predicted_expectancy_usd=9.0,
        bootstrap={"ci_low": -5.0, "ci_high": 6.0, "excludes_zero": False},
    )
    assert excluded.verdict == KILL and excluded.reason_code == "negative_expectation"


def test_a_thin_sample_cannot_kill():
    d = decision(trades=MIN_TRADES_FOR_VERDICT - 1, expectancy_usd=-9.0)

    assert d.verdict == KEEP and d.reason_code == "insufficient_evidence"
    assert not d.acts


def test_a_missing_expectancy_cannot_kill():
    d = decision(expectancy_usd=None)

    assert d.verdict == KEEP and d.reason_code == "insufficient_evidence"


# --- criterion 2: CVaR share growing without a return share ----------------
def test_a_rising_cvar_share_without_a_return_share_kills():
    d = decision(expectancy_usd=1.0, cvar_share=0.42, return_share=0.11)

    assert d.verdict == KILL and d.reason_code == "cvar_grows_without_return"


def test_a_return_share_that_covers_the_cvar_does_not_kill():
    d = decision(expectancy_usd=1.0, cvar_share=0.30, return_share=0.45)

    assert d.verdict == KEEP


# --- criterion 3: positive but not distinguishable -------------------------
def test_a_positive_result_whose_interval_touches_zero_shrinks():
    d = decision(
        expectancy_usd=2.0,
        bootstrap={"ci_low": -1.0, "ci_high": 5.0, "excludes_zero": False},
        shrink_pct=0.10,
    )

    assert d.verdict == SHRINK and d.reason_code == "weak_risk_adjusted_contribution"
    assert d.acts and d.target_pct == 0.10


def test_a_positive_result_clear_of_zero_keeps_the_sleeve():
    d = decision(
        expectancy_usd=2.0,
        bootstrap={"ci_low": 0.5, "ci_high": 4.0, "excludes_zero": True},
    )

    assert d.verdict == KEEP and d.reason_code == "keep"
    assert d.target_pct == 0.30


# --- the report ------------------------------------------------------------
def test_the_review_report_names_the_verdicts_n_and_the_claim_limit(tmp_path):
    decisions = [
        decision(expectancy_usd=-5.0),
        decision(sleeve="swing", expectancy_usd=6.0),
    ]

    path = write_review_report(
        tmp_path / "review",
        decisions=decisions,
        generated_at=datetime(2026, 12, 1, 17, 0),
        trials=9,
    )

    markdown = path.read_text(encoding="utf-8")
    assert "Trials (N): 9" in markdown
    assert "feasibility, not superiority" in markdown
    assert "**kill**" in markdown and "**keep**" in markdown
    payload = json.loads((tmp_path / "review.json").read_text(encoding="utf-8"))
    assert payload["decisions"][0]["reason_code"] == "negative_expectation"
    assert payload["artifact_sha256"]


def test_the_criteria_are_evaluated_in_a_fixed_order():
    """Every criterion is checked in REVIEW_CRITERIA order: the first match wins."""
    from signald.lineage import REVIEW_CRITERIA

    assert REVIEW_CRITERIA[0] == "negative_expectation"
    assert REVIEW_CRITERIA[-1] == "keep"
    both = decision(expectancy_usd=-5.0, cvar_share=0.9, return_share=0.1)
    assert both.reason_code == "negative_expectation"
