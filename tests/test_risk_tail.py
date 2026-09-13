"""P1 tail tests: ES/CVaR estimators and the worst-of-three aggregate (plan §6.5).

Every formula is checked against arithmetic worked by hand in the test, not copied
from the implementation - the phase gate requires "ES/vol formulas match
hand-worked examples" (plan §8, P1). The aggregation tests are also the mutation
proof for this module: replacing ``max`` with ``min`` in ``es_estimate`` /
``es_stress`` makes them fail (see ``test_estimate_is_the_maximum_not_the_minimum``
and ``test_stress_takes_the_worst_shock_not_the_mildest``).
"""

from __future__ import annotations

import math

import pytest

from signald.risk.tail import (
    StressGrid,
    es_estimate,
    es_historical,
    es_parametric,
    es_stress,
    z_score,
)

pytestmark = pytest.mark.timeout(120)

#: Ten 1-day losses, ascending, as fractions of equity.
LOSSES_10 = [0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09]


# --- normal quantile -------------------------------------------------------
@pytest.mark.parametrize(
    ("confidence", "expected"),
    [(0.975, 1.960), (0.95, 1.645), (0.99, 2.326)],
)
def test_z_score_matches_the_normal_table_to_three_dp(confidence, expected):
    assert round(z_score(confidence), 3) == expected


def test_z_score_rejects_a_value_that_is_not_a_probability():
    with pytest.raises(ValueError):
        z_score(1.0)


# --- historical ES ---------------------------------------------------------
def test_historical_es_is_the_mean_of_the_worst_ceil_tail():
    # Hand arithmetic: n = 10, confidence = 0.8
    #   tail = ceil((1 - 0.8) * 10) = ceil(2.0) = 2
    #   worst two losses = 0.09 and 0.08
    #   ES = (0.09 + 0.08) / 2 = 0.085
    assert es_historical(LOSSES_10, 0.8) == pytest.approx(0.085)


def test_historical_es_at_975_uses_the_single_worst_loss():
    # tail = ceil((1 - 0.975) * 10) = ceil(0.25) = 1 -> the worst loss, 0.09.
    assert es_historical(LOSSES_10, 0.975) == pytest.approx(0.09)


def test_historical_es_ignores_the_quiet_part_of_the_series():
    # Adding a quiet observation changes the tail count, not the worst loss:
    # n = 11 -> tail = ceil(0.025 * 11) = 1; the extra 0.00 must not dilute it.
    assert es_historical([*LOSSES_10, 0.00], 0.975) == pytest.approx(0.09)


def test_historical_es_of_an_empty_series_raises():
    with pytest.raises(ValueError):
        es_historical([], 0.975)


def test_historical_es_rejects_a_return_series():
    # A negative loss is a gain: the caller passed returns, and the sign must not
    # be guessed (fail closed).
    with pytest.raises(ValueError):
        es_historical([-0.01, 0.02], 0.975)


# --- parametric ES ---------------------------------------------------------
def test_parametric_es_is_z_times_sigma_at_horizon_one():
    # z(0.975) = 1.959964; 1.959964 * 0.02 = 0.0391993.
    assert es_parametric(0.02, 0.975) == pytest.approx(z_score(0.975) * 0.02)
    assert es_parametric(0.02, 0.975) == pytest.approx(0.0391993, abs=1e-7)


def test_parametric_es_scales_with_the_square_root_of_horizon():
    # sqrt(4) = 2, so a 4-day horizon is twice the 1-day ES.
    assert es_parametric(0.02, 0.975, 4) == pytest.approx(2 * es_parametric(0.02, 0.975))


def test_parametric_es_rejects_a_zero_horizon():
    with pytest.raises(ValueError):
        es_parametric(0.02, 0.975, 0)


# --- stress grid -----------------------------------------------------------
def test_stress_takes_the_worst_shock_not_the_mildest():
    # Default grid, weight 1.0, small sigma: the halt shock 0.20 dominates the
    # gap shock 0.10 and the vol shock.
    assert es_stress(0.001, 0.975, weight=1.0) == pytest.approx(0.20)
    assert es_stress(0.001, 0.975, weight=1.0) == pytest.approx(
        max(es_parametric(0.001 * 2.0, 0.975, 1), 0.10, 0.20)
    )


def test_stress_vol_shock_is_the_parametric_es_at_doubled_vol():
    # Zero the scenario shocks to isolate the vol shock: 2 * z * sigma.
    vol_only = StressGrid(vol_mult=2.0, gap_pct=0.0, halt_pct=0.0)
    assert es_stress(0.02, 0.975, grid=vol_only) == pytest.approx(
        2 * es_parametric(0.02, 0.975)
    )


def test_stress_vol_shock_respects_the_grid_horizon():
    vol_only = StressGrid(vol_mult=1.0, gap_pct=0.0, halt_pct=0.0, horizon_days=4)
    assert es_stress(0.02, 0.975, grid=vol_only) == pytest.approx(
        es_parametric(0.02, 0.975, 4)
    )


def test_weight_scales_gap_and_halt_shocks_but_not_the_vol_shock():
    scenario = StressGrid(vol_mult=1.0, gap_pct=0.10, halt_pct=0.20)
    # sigma tiny -> the vol shock (z * 0.001 ~ 0.00196) is below both scenario
    # shocks; at weight 0.25 the halt shock is 0.20 * 0.25 = 0.05.
    assert es_stress(0.001, 0.975, grid=scenario, weight=0.25) == pytest.approx(0.05)
    assert es_stress(0.001, 0.975, grid=scenario, weight=1.0) == pytest.approx(0.20)
    # With the scenarios zeroed the vol shock is all that remains, and it is
    # weight-independent.
    vol_only = StressGrid(vol_mult=1.0, gap_pct=0.0, halt_pct=0.0)
    assert es_stress(0.02, 0.975, grid=vol_only, weight=0.25) == pytest.approx(
        es_stress(0.02, 0.975, grid=vol_only, weight=1.0)
    )
    assert es_stress(0.02, 0.975, grid=vol_only, weight=0.25) == pytest.approx(
        z_score(0.975) * 0.02
    )


def test_weight_is_clamped_to_the_unit_interval():
    grid = StressGrid(vol_mult=0.0, gap_pct=0.10, halt_pct=0.20)
    assert es_stress(0.0, 0.975, grid=grid, weight=5.0) == pytest.approx(0.20)
    assert es_stress(0.0, 0.975, grid=grid, weight=-3.0) == pytest.approx(0.0)


# --- the estimator selector ------------------------------------------------
def test_estimate_is_the_maximum_not_the_minimum():
    # Three distinct components: historical 0.09, parametric 0.0588, stress 0.20.
    result = es_estimate(LOSSES_10, confidence=0.975, sigma_daily=0.03)
    assert result.components["historical"] == pytest.approx(0.09)
    assert result.value_pct == pytest.approx(0.20)
    assert result.value_pct == max(result.components.values())
    assert result.value_pct > min(result.components.values())
    assert set(result.components) == {"historical", "parametric", "stress"}


def test_estimate_names_the_binding_estimator():
    result = es_estimate(LOSSES_10, confidence=0.975, sigma_daily=0.03)
    assert result.components[result.estimator] == pytest.approx(result.value_pct)
    assert result.estimator == "stress"


def test_estimate_historical_binds_when_the_tail_loss_is_the_largest():
    # Constant large losses -> sigma 0, so only the historical estimate is large.
    result = es_estimate([0.5] * 30, confidence=0.975, min_observations=30)
    assert result.estimator == "historical"
    assert result.value_pct == pytest.approx(0.5)


def test_estimate_parametric_binds_when_stress_is_relaxed_below_it():
    grid = StressGrid(vol_mult=0.5, gap_pct=0.0, halt_pct=0.0)
    result = es_estimate(
        [0.01, 0.02], confidence=0.975, sigma_daily=0.1, grid=grid, min_observations=2
    )
    # historical: ceil(0.025 * 2) = 1 -> 0.02; parametric: 1.959964 * 0.1 = 0.195996.
    assert result.estimator == "parametric"
    assert result.value_pct == pytest.approx(z_score(0.975) * 0.1)
    assert result.flagged is False


def test_stress_binds_when_volatility_doubles():
    # Zero the gap/halt scenarios and double vol: 2 * z * 0.03 = 0.117598, above
    # the historical worst loss (0.09) and the unstressed parametric value.
    grid = StressGrid(vol_mult=2.0, gap_pct=0.0, halt_pct=0.0)
    result = es_estimate(LOSSES_10, confidence=0.975, sigma_daily=0.03, grid=grid)
    assert result.estimator == "stress"
    assert result.value_pct == pytest.approx(2 * es_parametric(0.03, 0.975))
    assert result.value_pct > result.components["parametric"]


# --- sigma sourcing and the flag -------------------------------------------
def test_sigma_defaults_to_the_sample_stdev_of_losses():
    losses = [0.01, 0.03]
    # Two-point sample stdev: sqrt(((0.01-0.02)^2 + (0.03-0.02)^2) / 1) = sqrt(0.0002).
    sigma = math.sqrt(0.0002)
    result = es_estimate(losses, confidence=0.975, min_observations=2)
    assert result.components["parametric"] == pytest.approx(z_score(0.975) * sigma)
    assert result.window_days == 2


def test_explicit_sigma_overrides_the_history_derived_sigma():
    losses = [0.01, 0.03]
    result = es_estimate(losses, confidence=0.975, sigma_daily=0.5, min_observations=2)
    assert result.components["parametric"] == pytest.approx(z_score(0.975) * 0.5)


def test_insufficient_history_flags_without_inventing_a_number():
    losses = [0.01, 0.02, 0.03, 0.04, 0.05]  # 5 observations, below the 30 minimum
    result = es_estimate(losses, confidence=0.975)
    assert result.flagged is True
    assert result.window_days == 5
    # The number is the stress grid's, not a fabricated historical estimate:
    # tail = ceil(0.025 * 5) = 1 -> historical is the worst loss, 0.05.
    assert result.components["historical"] == pytest.approx(0.05)
    assert result.value_pct == pytest.approx(result.components["stress"])
    assert result.value_pct == pytest.approx(0.20)


def test_enough_history_with_a_live_sigma_is_not_flagged():
    result = es_estimate(
        LOSSES_10, confidence=0.975, sigma_daily=0.03, min_observations=10
    )
    assert result.flagged is False
    assert result.window_days == 10


def test_zero_sigma_with_no_history_returns_the_stress_value_not_zero():
    result = es_estimate([], confidence=0.975, sigma_daily=0.0)
    assert result.value_pct == pytest.approx(0.20)
    assert result.value_pct > 0.0
    assert result.estimator == "stress"
    assert result.flagged is True
    assert result.window_days == 0
    assert result.components == {"historical": 0.0, "parametric": 0.0, "stress": 0.20}
