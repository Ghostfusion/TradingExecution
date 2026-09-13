"""The cost model and cost gate: hand-worked impact, band selection, strict k (plan §6)."""

from __future__ import annotations

import pytest

from signald.signals.costgate import (
    REASON_BELOW,
    CostBreakdown,
    check_cost_gate,
    expected_move_bps,
    impact_bps,
    round_trip_cost,
)

pytestmark = pytest.mark.timeout(120)


def breakdown(**kw) -> CostBreakdown:
    base = {
        "spread_bps": 6.0,
        "impact_bps": 2.0,
        "fees_bps": 2.0,
        "total_bps": 10.0,
        "low_float": False,
    }
    base.update(kw)
    return CostBreakdown(**base)


# --------------------------------------------------------------------------
# impact_bps (plan formula 6)
# --------------------------------------------------------------------------
def test_impact_matches_a_hand_worked_square_root_example():
    # 10_000 * 0.75 * 0.02 * sqrt(2_500_000 / 100_000_000)
    # = 150 * sqrt(0.025) = 150 * 0.158113883 = 23.7171 bps
    got = impact_bps(notional_usd=2_500_000.0, adv_usd=100_000_000.0, sigma_daily=0.02)
    assert got == pytest.approx(23.7171, abs=1e-3)


def test_impact_is_monotonic_in_order_size():
    small = impact_bps(notional_usd=100_000.0, adv_usd=50_000_000.0, sigma_daily=0.015)
    large = impact_bps(notional_usd=1_000_000.0, adv_usd=50_000_000.0, sigma_daily=0.015)
    assert 0.0 < small < large


def test_impact_zero_size_is_zero_and_unknown_adv_is_infinite():
    assert impact_bps(notional_usd=0.0, adv_usd=1e8, sigma_daily=0.02) == 0.0
    assert impact_bps(notional_usd=10_000.0, adv_usd=0.0, sigma_daily=0.02) == float("inf")


# --------------------------------------------------------------------------
# round_trip_cost (plan formula 7)
# --------------------------------------------------------------------------
def test_round_trip_uses_the_liquid_band(cfg):
    # ADV 2M shares > min_adv_shares(1M) -> liquid band (12 bps); impact 1.5 bps.
    c = round_trip_cost(
        spread_bps=None,
        notional_usd=10_000.0,
        adv_shares=2_000_000.0,
        price=50.0,
        atr_pct=0.02,
        config=cfg,
    )
    assert c.low_float is False
    assert c.spread_bps == cfg.cost_liquid_bps == 12.0
    assert c.impact_bps == pytest.approx(1.5, abs=1e-6)
    assert c.fees_bps == 0.0
    assert c.total_bps == pytest.approx(13.5, abs=1e-6)


def test_round_trip_uses_the_wide_band_below_min_adv(cfg):
    # ADV 0.5M shares < 1M -> low-float band (30 bps); impact 3.0 bps.
    c = round_trip_cost(
        spread_bps=None,
        notional_usd=10_000.0,
        adv_shares=500_000.0,
        price=50.0,
        atr_pct=0.02,
        config=cfg,
    )
    assert c.low_float is True
    assert c.spread_bps == cfg.cost_lowfloat_bps == 30.0
    assert c.impact_bps == pytest.approx(3.0, abs=1e-6)
    assert c.total_bps == pytest.approx(33.0, abs=1e-6)


def test_round_trip_treats_an_unknown_adv_as_low_float(cfg):
    c = round_trip_cost(
        spread_bps=None,
        notional_usd=10_000.0,
        adv_shares=None,
        price=50.0,
        atr_pct=0.02,
        config=cfg,
    )
    assert c.low_float is True and c.spread_bps == cfg.cost_lowfloat_bps
    assert c.impact_bps == 0.0  # no denominator -> no estimate; the wide band stands in


def test_round_trip_passes_a_known_spread_through_unchanged(cfg):
    c = round_trip_cost(
        spread_bps=5.0,
        notional_usd=10_000.0,
        adv_shares=2_000_000.0,
        price=None,  # no price -> impact not estimated
        atr_pct=0.02,
        config=cfg,
    )
    assert c.spread_bps == 5.0
    assert c.impact_bps == 0.0
    assert c.total_bps == 5.0
    assert c.low_float is False


# --------------------------------------------------------------------------
# check_cost_gate (plan formula 7)
# --------------------------------------------------------------------------
def test_cost_gate_is_strict_at_the_required_multiple(cfg):
    cost = breakdown(total_bps=10.0)  # 3x -> 30 bps
    at = check_cost_gate(expected_move_bps=30.0, cost=cost, config=cfg)
    assert not at.ok and at.reason == "move_below_cost"
    above = check_cost_gate(expected_move_bps=30.01, cost=cost, config=cfg)
    assert above.ok and above.reason == "ok"
    below = check_cost_gate(expected_move_bps=29.99, cost=cost, config=cfg)
    assert not below.ok


def test_cost_gate_refuses_unknown_inputs(cfg):
    no_cost = check_cost_gate(expected_move_bps=100.0, cost=None, config=cfg)
    assert (no_cost.ok, no_cost.reason) == (False, "cost_unknown")
    no_move = check_cost_gate(expected_move_bps=None, cost=breakdown(), config=cfg)
    assert (no_move.ok, no_move.reason) == (False, "move_unknown")
    # a known cost with an uneconomic move refuses through the gate itself
    uneconomic = check_cost_gate(expected_move_bps=0.0, cost=breakdown(), config=cfg)
    assert (uneconomic.ok, uneconomic.reason) == (False, REASON_BELOW)
    # a non-positive total is not a trustworthy cost either
    free = check_cost_gate(expected_move_bps=100.0, cost=breakdown(total_bps=0.0), config=cfg)
    assert (free.ok, free.reason) == (False, "cost_unknown")


def test_cost_gate_reports_the_multiple_and_both_sides(cfg):
    g = check_cost_gate(expected_move_bps=30.0, cost=breakdown(total_bps=10.0), config=cfg)
    assert g.required_multiple == cfg.cost_gate_multiple == 3.0
    assert g.round_trip_cost_bps == 10.0 and g.expected_move_bps == 30.0
    assert g.breakdown is not None


# --------------------------------------------------------------------------
# expected_move_bps (plan §5.2)
# --------------------------------------------------------------------------
def test_expected_move_is_one_atr_capped_and_halves_at_a_quarter_session():
    full = expected_move_bps(atr=2.0, price=100.0, hold_minutes=390.0)
    assert full == pytest.approx(200.0, abs=1e-9)  # (2/100) * 1e4, no shrink
    quarter = expected_move_bps(atr=2.0, price=100.0, hold_minutes=97.5)
    assert quarter == pytest.approx(100.0, abs=1e-9)  # sqrt(1/4) = 1/2
    assert expected_move_bps(atr=2.0, price=100.0, hold_minutes=100_000.0) == pytest.approx(full)


def test_expected_move_is_zero_for_a_nonpositive_horizon_or_atr():
    assert expected_move_bps(atr=2.0, price=100.0, hold_minutes=0.0) == 0.0
    assert expected_move_bps(atr=0.0, price=100.0, hold_minutes=60.0) == 0.0
