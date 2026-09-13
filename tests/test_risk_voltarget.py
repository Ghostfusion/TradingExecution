"""Vol estimator + target scalar tests (plan §6 formulas 1-2, design §10).

The hand-worked examples below write out the arithmetic (lambda, variance,
annualisation) rather than copying a value from the implementation, so a
regression in either formula fails the test.
"""

from __future__ import annotations

import math

import pytest

from signald.risk.voltarget import VolScalar, ewma_vol, vol_scalar

pytestmark = pytest.mark.timeout(120)

#: lambda = ln(2) / halflife, so this half-life makes lambda exactly 1/2.
HALFLIFE_HALF = 2.0 * math.log(2.0)


# --- formula 1: EWMA volatility -------------------------------------------
def test_ewma_sigma_matches_hand_worked_example():
    # lambda = ln(2) / (2 ln 2) = 1/2. Returns, most recent first:
    #   r0 = 0.02, r1 = -0.01, r2 = 0.03
    # var = (1 - 1/2) * (1/2^0 * 0.02^2 + 1/2^1 * 0.01^2 + 1/2^2 * 0.03^2)
    #     = 0.5 * (0.0004 + 0.00005 + 0.000225)
    #     = 0.0003375
    # sigma_annual = sqrt(0.0003375 * 252) = 0.2916333314...
    expected = math.sqrt(0.0003375 * 252)
    assert expected == pytest.approx(0.29163333, abs=1e-8)
    assert ewma_vol([0.02, -0.01, 0.03], halflife_days=HALFLIFE_HALF) == pytest.approx(
        expected
    )


def test_ewma_weights_the_most_recent_observation_first():
    # Reversing the same series changes the weights (1/2^i on i = 0 first):
    # [0.03, -0.01, 0.02] -> var = 0.5*(0.0009 + 0.00005 + 0.0001) = 0.000525
    recent_first = ewma_vol([0.03, -0.01, 0.02], halflife_days=HALFLIFE_HALF)
    assert recent_first == pytest.approx(math.sqrt(0.000525 * 252))
    assert recent_first != pytest.approx(
        ewma_vol([0.02, -0.01, 0.03], halflife_days=HALFLIFE_HALF)
    )


def test_ewma_uses_squared_returns_so_sign_is_irrelevant():
    assert ewma_vol([0.02, -0.01, 0.03], halflife_days=HALFLIFE_HALF) == pytest.approx(
        ewma_vol([-0.02, 0.01, -0.03], halflife_days=HALFLIFE_HALF)
    )


def test_ewma_annualises_by_periods_per_year():
    # Single return 0.01: var = 0.5 * 0.01^2; sigma_1 = sqrt(0.5)*0.01.
    per_period = ewma_vol([0.01], halflife_days=HALFLIFE_HALF, periods_per_year=1)
    annual = ewma_vol([0.01], halflife_days=HALFLIFE_HALF, periods_per_year=252)
    assert per_period == pytest.approx(math.sqrt(0.5) * 0.01)
    assert annual == pytest.approx(per_period * math.sqrt(252))


def test_ewma_of_an_empty_series_is_zero():
    assert ewma_vol([]) == 0.0
    assert ewma_vol([], periods_per_year=1) == 0.0


@pytest.mark.parametrize("bad", [0.0, -1.0, -20.0])
def test_ewma_rejects_a_non_positive_halflife(bad):
    with pytest.raises(ValueError):
        ewma_vol([0.01], halflife_days=bad)


def test_ewma_rejects_a_non_positive_periods_per_year():
    with pytest.raises(ValueError):
        ewma_vol([0.01], periods_per_year=0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_ewma_fails_closed_on_a_non_finite_return(bad):
    with pytest.raises(ValueError):
        ewma_vol([0.01, bad])


# --- formula 2: vol-target scalar -----------------------------------------
def test_warmup_returns_no_scaling():
    result = vol_scalar([0.02] * 269, target=0.10)
    assert isinstance(result, VolScalar)
    assert result.scalar == 1.0
    assert result.applied is False
    assert result.warmup_ok is False
    assert result.reason == "warmup"
    assert result.target == 0.10


def test_warmup_takes_precedence_over_zero_vol():
    result = vol_scalar([0.0] * 5, target=0.10, warmup_days=270)
    assert result.reason == "warmup"
    assert result.scalar == 1.0
    assert result.warmup_ok is False


def test_scaling_is_allowed_at_exactly_warmup_days():
    # 270 x 0.01: EWMA weights sum to 1, so realized = 0.01 * sqrt(252) = 0.1587.
    # raw = 0.10 / 0.1587 = 0.6299, a 37% move from current -> applied.
    result = vol_scalar([0.01] * 270, target=0.10, warmup_days=270, current=1.0)
    assert result.warmup_ok is True
    assert result.applied is True
    assert result.reason == "applied"
    assert result.scalar == pytest.approx(0.10 / (0.01 * math.sqrt(252)))


def test_cap_binds_for_a_very_low_realized_vol():
    # realized = 0.001 * sqrt(126) = 0.011225 -> raw = 8.91, clamped to cap 1.5.
    result = vol_scalar(
        [0.001], target=0.10, halflife_days=HALFLIFE_HALF, warmup_days=1, current=1.0
    )
    assert result.realized == pytest.approx(0.001 * math.sqrt(126))
    assert result.scalar == 1.5
    assert result.applied is True
    assert result.reason == "applied"


def test_floor_binds_for_a_very_high_realized_vol():
    # realized = 0.5 * sqrt(126) = 5.6125 -> raw = 0.0178, clamped up to floor 0.10.
    result = vol_scalar(
        [0.5], target=0.10, halflife_days=HALFLIFE_HALF, warmup_days=1, current=1.0
    )
    assert result.scalar == 0.10
    assert result.applied is True
    assert result.reason == "applied"


def test_band_suppresses_a_small_change_and_keeps_current():
    # realized = 0.01 * sqrt(126) = 0.11225 -> raw = 0.890871.
    # |0.890871 - 1.0| / 1.0 = 0.109129 <= band 0.12 -> suppressed.
    result = vol_scalar(
        [0.01], target=0.10, halflife_days=HALFLIFE_HALF, warmup_days=1, band=0.12, current=1.0
    )
    assert result.scalar == 1.0
    assert result.applied is False
    assert result.reason == "within_band"


def test_a_large_change_is_applied():
    # realized = 0.02 * sqrt(126) = 0.2245 -> raw = 0.4454; a 55% move > band.
    raw = 0.10 / (0.02 * math.sqrt(126))
    result = vol_scalar(
        [0.02], target=0.10, halflife_days=HALFLIFE_HALF, warmup_days=1, band=0.12, current=1.0
    )
    assert result.scalar == pytest.approx(raw)
    assert result.applied is True
    assert result.reason == "applied"


def test_current_is_honoured_when_the_change_stays_inside_the_band():
    # raw = 0.890871 vs current 0.8: |0.890871 - 0.8| / 0.8 = 0.1136 <= 0.12.
    result = vol_scalar(
        [0.01], target=0.10, halflife_days=HALFLIFE_HALF, warmup_days=1, band=0.12, current=0.8
    )
    assert result.scalar == 0.8
    assert result.applied is False
    assert result.reason == "within_band"


def test_zero_realized_vol_returns_cap_without_raising():
    result = vol_scalar([0.0] * 270, target=0.10)
    assert result.realized == 0.0
    assert result.warmup_ok is True
    assert result.scalar == 1.5
    assert result.applied is False
    assert result.reason == "zero_vol"


def test_zero_realized_vol_returns_the_configured_cap():
    result = vol_scalar([0.0] * 270, target=0.10, cap=2.0)
    assert result.scalar == 2.0
    assert result.reason == "zero_vol"


@pytest.mark.parametrize("r", [0.0001, 0.001, 0.01, 0.1, 0.5, 2.0])
def test_the_computed_scalar_is_clamped_to_floor_and_cap(r):
    # band 0.0 makes every non-zero change apply, exposing the raw clamp.
    result = vol_scalar(
        [r], target=0.10, halflife_days=HALFLIFE_HALF, warmup_days=1, band=0.0, current=1.0
    )
    realized = abs(r) * math.sqrt(126)
    assert result.scalar == pytest.approx(min(1.5, max(0.10, 0.10 / realized)))
    assert 0.10 <= result.scalar <= 1.5


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_non_positive_halflife_raises_in_both_functions(bad):
    with pytest.raises(ValueError):
        ewma_vol([0.01], halflife_days=bad)
    with pytest.raises(ValueError):
        vol_scalar([0.01], target=0.10, halflife_days=bad, warmup_days=1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"target": 0.0},
        {"target": -0.10},
        {"floor": 0.0},
        {"cap": 0.0},
        {"floor": 1.6, "cap": 1.5},
        {"band": -0.01},
        {"current": 0.0},
        {"warmup_days": -1},
    ],
)
def test_invalid_scalar_parameters_fail_closed(overrides):
    kwargs = {"target": 0.10, "warmup_days": 1}
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        vol_scalar([0.01], **kwargs)
