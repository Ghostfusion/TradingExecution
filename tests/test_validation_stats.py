"""P4 validation-statistics tests: every formula checked against hand-worked arithmetic.

Plan §11.1-11.2 / design §11: the numbers that decide whether a backtest means
anything. Each test states the arithmetic it relies on; a statistic that cannot
be computed must surface as unavailable (``None``), never as a fabricated number.
"""

from __future__ import annotations

import math
import random
import statistics

import pytest

from signald.validation.stats import (
    DSR_THRESHOLD,
    MIN_OOS_MONTHS,
    MIN_TRADES,
    PBO_THRESHOLD,
    ValidationGate,
    deflated_sharpe,
    minimum_backtest_length,
    newey_west_t,
    paired_comparison,
    probability_of_backtest_overfitting,
    stationary_bootstrap_ci,
    validation_gates,
)

pytestmark = pytest.mark.timeout(120)

#: A decaying edge: the first 40 observations mean +0.02, the last 40 mean -0.01
#: (the intra-period wobble supplies the variance a Sharpe needs).
BETTER_FIRST_HALF = [0.02 + 0.002 * (i % 5 - 2) for i in range(40)] + [
    -0.01 + 0.002 * (i % 5 - 2) for i in range(40)
]

#: A stationary, seeded series: no regime change, so PBO sits near its 0.5 baseline.
_STATIONARY = []
_rng = random.Random(0)
for _ in range(80):
    _STATIONARY.append(_rng.gauss(0.0, 0.01))


# ---------------------------------------------------------------------------
# deflated Sharpe
# ---------------------------------------------------------------------------
def test_deflated_sharpe_hand_worked():
    # Inputs: sharpe=1.0, n_trials=2, observations=4, skew=0, kurtosis=3.
    #   var_sr          = 1/4 = 0.25, sqrt(var_sr) = 0.5
    #   E[max_2]        = 0.4227843*z^-1(0.5) + 0.5772157*z^-1(1 - 1/(2e))
    #                   = 0.4227843*0 + 0.5772157*0.899797 = 0.519755
    #   sr0             = 0.5 * 0.519755 = 0.259878
    #   correction      = 1 - 0*1 + (3-1)/4*1^2 = 1.5, sqrt = 1.224745
    #   z               = (1 - 0.259878) * sqrt(3) / 1.224745 = 1.046696
    #   DSR             = Phi(1.046696) = 0.852379
    assert deflated_sharpe(sharpe=1.0, n_trials=2, observations=4) == pytest.approx(
        0.852379, abs=1e-5
    )


def test_deflated_sharpe_is_a_probability():
    value = deflated_sharpe(sharpe=0.5, n_trials=10, observations=50, skew=-0.5, kurtosis=6.0)
    assert 0.0 < value < 1.0


def test_deflated_sharpe_falls_as_trials_grow():
    # Same Sharpe and sample; only the multiple-testing penalty changes.
    # sharpe=0.3, observations=16 -> DSR(1)=0.8721 > DSR(2)=0.7403 > DSR(500)=0.0397.
    single = deflated_sharpe(sharpe=0.3, n_trials=1, observations=16)
    weak = deflated_sharpe(sharpe=0.3, n_trials=2, observations=16)
    many = deflated_sharpe(sharpe=0.3, n_trials=500, observations=16)
    assert single > weak > many
    assert many < DSR_THRESHOLD


def test_deflated_sharpe_of_zero_sharpe_is_half_no_evidence():
    # One trial (no selection), zero observed Sharpe: sr0 = 0 and the numerator is
    # 0, so DSR = Phi(0) = 0.5 exactly - a coin flip, not a pass.
    assert deflated_sharpe(sharpe=0.0, n_trials=1, observations=100) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sharpe": 1.0, "n_trials": 0, "observations": 10},
        {"sharpe": 1.0, "n_trials": 5, "observations": 1},
    ],
)
def test_deflated_sharpe_rejects_unusable_inputs(kwargs):
    with pytest.raises(ValueError):
        deflated_sharpe(**kwargs)


# ---------------------------------------------------------------------------
# probability of backtest overfitting
# ---------------------------------------------------------------------------
def test_pbo_flags_a_decaying_edge():
    # The first half is strictly better than the second: every in/out split sees
    # an out-of-sample Sharpe below its in-sample Sharpe -> PBO = 1.0.
    assert probability_of_backtest_overfitting(BETTER_FIRST_HALF) == pytest.approx(1.0)


def test_pbo_of_a_stationary_series_is_near_the_baseline():
    # A seeded, stationary series has no regime change: ~half the walk-forward
    # splits lose out of sample. 0.5714 is seed 0's value (mean over seeds ~0.51).
    pbo = probability_of_backtest_overfitting(_STATIONARY)
    assert pbo == pytest.approx(0.5714285714285714)
    assert 0.25 <= pbo <= 0.75
    assert pbo < probability_of_backtest_overfitting(BETTER_FIRST_HALF)


def test_pbo_is_deterministic():
    assert probability_of_backtest_overfitting(_STATIONARY) == probability_of_backtest_overfitting(
        _STATIONARY
    )


def test_pbo_returns_none_when_there_are_too_few_observations():
    # Fewer than splits*4 = 32 observations: honestly unavailable, not zero.
    assert probability_of_backtest_overfitting([0.01, -0.01] * 10, splits=8) is None


@pytest.mark.parametrize(
    "call",
    [
        lambda: probability_of_backtest_overfitting([]),
        lambda: probability_of_backtest_overfitting([0.1] * 40, splits=1),
        lambda: probability_of_backtest_overfitting([0.1] * 40, trials=0),
    ],
)
def test_pbo_rejects_unusable_inputs(call):
    with pytest.raises(ValueError):
        call()


# ---------------------------------------------------------------------------
# minimum backtest length
# ---------------------------------------------------------------------------
def test_minimum_backtest_length_hand_worked():
    # Plan §6 formula 11: MinBTL = (E[max_N]/SR*)^2 years. At N=5,
    # E[max_5] = 1.19248 -> (1.19248/1.0)^2 = 1.4223 years (~1.4). The upper-bound
    # simplification 2*ln(5) = 3.219 is larger, as it must be.
    assert minimum_backtest_length(n_trials=5, sharpe=1.0) == pytest.approx(1.4222804513)
    assert minimum_backtest_length(n_trials=5, sharpe=1.0) < 2 * math.log(5)


def test_minimum_backtest_length_halves_with_double_the_sharpe():
    assert minimum_backtest_length(n_trials=5, sharpe=2.0) == pytest.approx(
        minimum_backtest_length(n_trials=5, sharpe=1.0) / 4
    )


def test_minimum_backtest_length_of_a_single_trial_is_zero():
    assert minimum_backtest_length(n_trials=1, sharpe=1.0) == 0.0


@pytest.mark.parametrize("sharpe", [0.0, -0.5])
def test_minimum_backtest_length_is_infinite_without_a_positive_sharpe(sharpe):
    assert minimum_backtest_length(n_trials=5, sharpe=sharpe) == math.inf


def test_minimum_backtest_length_rejects_zero_trials():
    with pytest.raises(ValueError):
        minimum_backtest_length(n_trials=0, sharpe=1.0)


# ---------------------------------------------------------------------------
# stationary bootstrap
# ---------------------------------------------------------------------------
def _stationary_series() -> list[float]:
    return [math.sin(i / 3.0) * 0.01 + 0.001 for i in range(120)]


def test_stationary_bootstrap_interval_contains_the_sample_mean():
    values = _stationary_series()
    sample_mean = statistics.fmean(values)
    low, high = stationary_bootstrap_ci(values, statistic=statistics.fmean, resamples=500)
    assert low <= sample_mean <= high


def test_stationary_bootstrap_is_deterministic_for_a_fixed_seed():
    values = _stationary_series()
    first = stationary_bootstrap_ci(values, statistic=statistics.fmean, resamples=500, seed=7)
    second = stationary_bootstrap_ci(values, statistic=statistics.fmean, resamples=500, seed=7)
    assert first == second


def test_stationary_bootstrap_interval_is_ordered():
    low, high = stationary_bootstrap_ci(
        _stationary_series(), statistic=statistics.fmean, resamples=200
    )
    assert low < high


def test_stationary_bootstrap_of_empty_input_raises():
    with pytest.raises(ValueError):
        stationary_bootstrap_ci([], statistic=statistics.fmean)


# ---------------------------------------------------------------------------
# paired comparison
# ---------------------------------------------------------------------------
def test_paired_comparison_of_a_mean_shifted_pair_excludes_zero():
    a = [0.012] * 50
    b = [0.002] * 50
    result = paired_comparison(a, b)
    # Every paired difference is exactly +0.010, so the whole interval sits above 0.
    assert result["mean_diff"] == pytest.approx(0.01)
    assert result["ci_low"] == pytest.approx(0.01)
    assert result["ci_high"] == pytest.approx(0.01)
    assert result["n"] == 50
    assert result["share_positive"] == pytest.approx(1.0)
    assert result["excludes_zero"] is True


def test_paired_comparison_of_identical_series_straddles_zero():
    series = _stationary_series()
    result = paired_comparison(series, series)
    assert result["mean_diff"] == 0.0
    assert result["excludes_zero"] is False


def test_paired_comparison_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        paired_comparison([0.1, 0.2], [0.1])


def test_paired_comparison_of_empty_input_raises():
    with pytest.raises(ValueError):
        paired_comparison([], [])


# ---------------------------------------------------------------------------
# Newey-West HAC t
# ---------------------------------------------------------------------------
def test_newey_west_t_of_a_constant_series_is_zero():
    # Zero long-run variance: undefined-safe, reported as 0.0 (never a divide error).
    assert newey_west_t([0.01] * 40) == 0.0


def test_newey_west_t_grows_with_a_shift():
    base = [math.sin(i / 2.0) * 0.01 for i in range(120)]
    unshifted = newey_west_t(base)
    shifted = newey_west_t([x + 0.05 for x in base])
    more_shifted = newey_west_t([x + 0.1 for x in base])
    assert shifted > unshifted
    assert more_shifted > shifted


def test_newey_west_default_lag_rule():
    # floor(4*(120/100)**(2/9)) = 4: an explicit 4 must equal the default.
    values = [math.sin(i / 2.0) * 0.01 for i in range(120)]
    assert newey_west_t(values) == pytest.approx(newey_west_t(values, lags=4))


def test_newey_west_t_of_empty_input_raises():
    with pytest.raises(ValueError):
        newey_west_t([])


# ---------------------------------------------------------------------------
# the pre-registered gate set
# ---------------------------------------------------------------------------
def test_validation_gates_report_each_pre_registered_gate():
    gates = validation_gates(
        sharpe=1.0,
        n_trials=500,
        observations=504,
        trades=80,
        oos_months=8.0,
        returns=BETTER_FIRST_HALF,
    )
    assert tuple(g.name for g in gates) == ("dsr", "pbo", "trades", "oos_months", "n_trials")
    by_name = {g.name: g for g in gates}
    assert by_name["dsr"].threshold == pytest.approx(DSR_THRESHOLD)
    assert by_name["pbo"].threshold == pytest.approx(PBO_THRESHOLD)
    assert by_name["trades"].threshold == pytest.approx(MIN_TRADES)
    assert by_name["oos_months"].threshold == pytest.approx(MIN_OOS_MONTHS)
    assert by_name["n_trials"].threshold is None
    assert all(isinstance(g, ValidationGate) for g in gates)
    assert by_name["dsr"].value is not None
    assert by_name["pbo"].value is not None
    assert by_name["trades"].passed is True
    assert by_name["oos_months"].passed is True
    assert by_name["n_trials"].passed is True


def test_validation_gates_report_pbo_unavailable_without_returns():
    gates = validation_gates(
        sharpe=1.0, n_trials=500, observations=504, trades=80, oos_months=8.0, returns=None
    )
    pbo = next(g for g in gates if g.name == "pbo")
    assert pbo.value is None
    assert pbo.passed is None
    assert "no return series" in pbo.reason


def test_validation_gates_fail_closed_when_dsr_is_uncomputable():
    gates = validation_gates(
        sharpe=1.0, n_trials=0, observations=504, trades=80, oos_months=8.0, returns=None
    )
    by_name = {g.name: g for g in gates}
    assert by_name["dsr"].value is None
    assert by_name["dsr"].passed is None
    assert by_name["n_trials"].passed is False
    assert by_name["n_trials"].reason


def test_validation_gates_flag_a_short_and_thin_backtest():
    gates = validation_gates(
        sharpe=1.0, n_trials=500, observations=504, trades=10, oos_months=3.0, returns=None
    )
    by_name = {g.name: g for g in gates}
    assert by_name["trades"].passed is False
    assert by_name["oos_months"].passed is False
