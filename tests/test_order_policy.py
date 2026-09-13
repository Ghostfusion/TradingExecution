"""P2 child-order policy tests (plan §4.4, design §8.1–§8.2).

Every number here is worked by hand in the test body or the comment above it.
The tick rule itself is owned by ``signald.order.prices``; policy is tested
against it, not through a re-implementation.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from signald.order.policy import (
    EXECUTION_STYLES,
    deviation_bps,
    plan_entry,
    plan_exit,
    style_for,
)
from signald.order.prices import round_price
from signald.risk.state import MarketState, RiskRequest

pytestmark = pytest.mark.timeout(120)


def market(**over) -> MarketState:
    base: dict = {
        "symbol": "MSFT",
        "last": 50.0,
        "spread_bps": 8.0,
        "adv_shares": 1_000_000.0,
    }
    base.update(over)
    return MarketState(**base)


def request(**over) -> RiskRequest:
    base: dict = {
        "sleeve": "intraday",
        "symbol": "MSFT",
        "setup": "ORB_RVOL",
        "side": "buy",
        "stop_distance": 5.0,
        "price": 50.0,
        "equity": 100_000.0,
        "requested_risk_pct": 0.01,
    }
    base.update(over)
    return RiskRequest(**base)


# --------------------------------------------------------------------------
# price precision (owned by prices.py, exercised as policy consumes it)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (1.00, 1.00),       # the boundary itself: 2dp grid
        (0.99, 0.99),       # below $1: 4dp grid, still exact
        (12.345, 12.35),    # 2dp, tie upward
        (0.98765, 0.9877),  # 4dp, tie upward
    ],
)
def test_round_price_boundaries(price, expected):
    assert round_price(price) == pytest.approx(expected)


@pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf")])
def test_round_price_rejects_non_finite(price):
    with pytest.raises(ValueError):
        round_price(price)


@pytest.mark.parametrize("tick", [0.0, -0.01])
def test_round_price_rejects_non_positive_tick(tick):
    with pytest.raises(ValueError):
        round_price(1.00, tick=tick)


# --------------------------------------------------------------------------
# deviation cap, worked by hand
# --------------------------------------------------------------------------
def test_deviation_cap_is_max_of_twice_spread_and_half_cost(cfg):
    # cost_liquid_bps=12 -> 6; spread 8 -> 2*8=16; floor 5 -> max = 16.
    assert deviation_bps(8.0, config=cfg) == pytest.approx(16.0)


def test_deviation_cap_floor_when_cost_band_is_tiny(cfg):
    # cost_liquid_bps=4 -> 2; spread unknown -> no invented widening; floor 5.
    assert deviation_bps(None, config=replace(cfg, cost_liquid_bps=4.0)) == pytest.approx(5.0)


def test_unknown_spread_never_widens_the_cap(cfg):
    # cost_liquid_bps=12 -> 6; a missing spread must not be guessed.
    assert deviation_bps(None, config=cfg) == pytest.approx(6.0)


def test_narrow_spread_loses_to_the_cost_band(cfg):
    # spread 1 -> 2, cost band -> 6, floor 5 -> 6.
    assert deviation_bps(1.0, config=cfg) == pytest.approx(6.0)


# --------------------------------------------------------------------------
# style selection
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("ORB_RVOL", "marketable_limit"),
        ("GAP_GO", "marketable_limit"),
        ("VWAP_PULLBACK", "marketable_limit"),
        ("vwap_revert", "resting_limit"),
        ("VWAP_REVERT", "resting_limit"),
        ("GAP_FADE", "resting_limit"),
        ("", "marketable_limit"),
        ("SOMETHING_NEW", "marketable_limit"),
    ],
)
def test_style_for_rule_table(setup, expected):
    assert style_for(setup) == expected


def test_unknown_setup_defaults_to_conservative_marketable(cfg):
    assert style_for("UNREGISTERED") == "marketable_limit"
    plan = plan_entry(request(setup="UNREGISTERED"), 100, market=market(), config=cfg)
    assert plan.style == "marketable_limit"


# --------------------------------------------------------------------------
# slicing: hand-worked counts, sizes, and the remainder
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("qty", "adv", "expected"),
    [
        # per_child = 0.05 * adv; count = min(5, max(1, ceil(qty / per_child)))
        (1000, 10000.0, (500, 500)),          # 1000/500 = 2
        (1000, 1000.0, (200, 200, 200, 200, 200)),  # 20 -> capped at 5
        (10, 200.0, (10,)),                   # 10/10 = 1
        (11, 100.0, (3, 3, 5)),               # 11/5 = 3 slices, last = 3 + 2
        (10, 50.0, (2, 2, 2, 4)),             # 10/2.5 = 4 slices, last = 2 + 2
        (3, 1.0, (1, 1, 1)),                  # 60 -> 5 -> reduced to qty
        (2, 1.0, (1, 1)),
        (1, 1000.0, (1,)),
    ],
)
def test_child_sizes_hand_worked(cfg, qty, adv, expected):
    plan = plan_entry(request(), qty, market=market(adv_shares=adv), config=cfg)
    assert tuple(o.qty for o in plan.orders) == expected
    assert sum(o.qty for o in plan.orders) == qty
    assert all(o.qty >= 1 for o in plan.orders)


def test_slice_count_capped_at_five(cfg):
    # 1000 shares into 0.05-share children would be 20,000 slices -> 5.
    plan = plan_entry(request(), 1000, market=market(adv_shares=1.0), config=cfg)
    assert len(plan.orders) == 5
    assert sum(o.qty for o in plan.orders) == 1000


def test_slice_count_never_exceeds_qty(cfg):
    plan = plan_entry(request(), 3, market=market(adv_shares=1.0), config=cfg)
    assert len(plan.orders) == 3
    assert tuple(o.qty for o in plan.orders) == (1, 1, 1)


@pytest.mark.parametrize("adv", [None, 0.0, float("nan")])
def test_unknown_adv_yields_one_child(cfg, adv):
    """The gate refuses orders without ADV; if reached anyway, do not slice."""
    plan = plan_entry(request(), 250, market=market(adv_shares=adv), config=cfg)
    assert len(plan.orders) == 1
    assert plan.orders[0].qty == 250


def test_child_seq_is_deterministic_zero_based(cfg):
    plan = plan_entry(request(), 11, market=market(adv_shares=100.0), config=cfg)
    assert tuple(o.seq for o in plan.orders) == (0, 1, 2)


def test_child_notional_is_qty_times_limit(cfg):
    plan = plan_entry(request(), 11, market=market(adv_shares=100.0), config=cfg)
    for o in plan.orders:
        assert o.notional_usd == pytest.approx(round(o.qty * o.limit_price, 2))


# --------------------------------------------------------------------------
# limit placement
# --------------------------------------------------------------------------
def test_breakout_buy_limit_is_above_the_market(cfg):
    plan = plan_entry(request(setup="ORB_RVOL"), 100, market=market(last=50.0), config=cfg)
    assert plan.style == "marketable_limit"
    assert plan.deviation_bps == pytest.approx(16.0)  # max(5, 6, 2*8)
    # 50 * (1 + 16/1e4) = 50.08
    assert plan.orders[0].limit_price == pytest.approx(50.08)
    assert all(o.limit_price > 50.0 for o in plan.orders)
    assert all(o.order_type == "marketable_limit" for o in plan.orders)


def test_breakout_sell_limit_is_below_the_market(cfg):
    plan = plan_entry(request(side="sell"), 100, market=market(last=50.0), config=cfg)
    assert all(o.limit_price < 50.0 for o in plan.orders)


def test_reversion_buy_never_crosses_the_market(cfg):
    plan = plan_entry(request(setup="VWAP_REVERT"), 100, market=market(last=50.0), config=cfg)
    assert plan.style == "resting_limit"
    assert all(o.order_type == "limit" for o in plan.orders)
    assert all(o.limit_price <= 50.0 for o in plan.orders)


def test_reversion_buy_off_grid_stays_passive(cfg):
    # round_price(12.345) would round *up* to 12.35; the resting buy must step
    # down to 12.34 instead of crossing the touch.
    plan = plan_entry(request(setup="GAP_FADE"), 10, market=market(last=12.345), config=cfg)
    assert plan.orders[0].limit_price == pytest.approx(12.34)
    assert plan.orders[0].limit_price <= 12.345


def test_reversion_sell_never_crosses_the_market(cfg):
    plan = plan_entry(request(setup="VWAP_REVERT", side="sell"), 100,
                      market=market(last=50.0), config=cfg)
    assert all(o.limit_price >= 50.0 for o in plan.orders)


def test_style_override_is_honoured(cfg):
    plan = plan_entry(request(setup="ORB_RVOL"), 100, market=market(), config=cfg,
                      style="resting_limit")
    assert plan.style == "resting_limit"
    assert all(o.order_type == "limit" for o in plan.orders)
    with pytest.raises(ValueError):
        plan_entry(request(), 100, market=market(), config=cfg, style="market")


def test_marketable_never_rounds_inside_the_spread(cfg):
    # A sub-$1 name: 0.50 * (1 + 16/1e4) = 0.5008; grid is 4dp.
    plan = plan_entry(request(setup="GAP_GO"), 10, market=market(last=0.50), config=cfg)
    assert plan.orders[0].limit_price > 0.50


# --------------------------------------------------------------------------
# exits
# --------------------------------------------------------------------------
def test_exit_is_a_marketable_limit(cfg):
    plan = plan_exit(request(setup="VWAP_REVERT", side="sell"), 100,
                     market=market(last=50.0), config=cfg)
    assert plan.style == "marketable_limit"
    assert plan.deviation_bps == pytest.approx(16.0)
    assert all(o.limit_price < 50.0 for o in plan.orders)
    assert all(o.order_type == "marketable_limit" for o in plan.orders)


def test_urgent_exit_widens_the_cap(cfg):
    calm = plan_exit(request(side="sell"), 100, market=market(last=50.0), config=cfg)
    urgent = plan_exit(request(side="sell"), 100, market=market(last=50.0), config=cfg, urgent=True)
    assert urgent.deviation_bps == pytest.approx(cfg.cost_lowfloat_bps)  # 30
    assert urgent.deviation_bps > calm.deviation_bps
    assert urgent.orders[0].limit_price < calm.orders[0].limit_price


# --------------------------------------------------------------------------
# fail closed
# --------------------------------------------------------------------------
@pytest.mark.parametrize("qty", [0, -1, -100])
def test_non_positive_qty_raises(cfg, qty):
    with pytest.raises(ValueError):
        plan_entry(request(), qty, market=market(), config=cfg)


@pytest.mark.parametrize("last", [None, float("nan"), 0.0, -5.0])
def test_missing_or_bad_price_raises(cfg, last):
    with pytest.raises(ValueError):
        plan_entry(request(), 100, market=market(last=last), config=cfg)


def test_exit_also_fails_closed(cfg):
    with pytest.raises(ValueError):
        plan_exit(request(), 0, market=market(), config=cfg)
    with pytest.raises(ValueError):
        plan_exit(request(), 10, market=market(last=None), config=cfg)


def test_only_the_two_legal_styles_are_ever_emitted(cfg):
    for setup in ("ORB_RVOL", "VWAP_REVERT"):
        plan = plan_entry(request(setup=setup), 100, market=market(), config=cfg)
        assert plan.style in EXECUTION_STYLES
        assert all(o.order_type in {"limit", "marketable_limit"} for o in plan.orders)
        assert all(o.style == plan.style for o in plan.orders)
