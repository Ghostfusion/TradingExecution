"""Synthetic stops: distance formula, mirror symmetry, cluster avoidance, legs.

Hand-worked example (design §10): with ``stop_atr_mult=1.75`` and ATR 2.00,
the ATR term is 3.50 and beats a 3.00 MAE floor; a 4.20 MAE floor beats it;
the distance can never fall below one ATR.
"""

from __future__ import annotations

import dataclasses

import pytest

from signald.order.guard import client_order_id
from signald.order.stops import (
    STOP_KIND,
    cluster_clearance,
    plan_stop,
    protective_orders,
    stop_distance,
)
from signald.risk.state import INTRADAY, MarketState, Position

pytestmark = pytest.mark.timeout(120)


def _market(**overrides) -> MarketState:
    return MarketState(symbol="AAA", **overrides)


# --------------------------------------------------------------------------
# stop_distance
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("atr", "mae_pctl", "expected"),
    [
        (2.0, 3.0, 3.5),  # 1.75 * 2.00 beats the 3.00 MAE floor
        (2.0, 4.2, 4.2),  # the MAE floor binds
        (2.0, 0.1, 3.5),  # a tiny MAE floor cannot tighten the ATR multiple
    ],
)
def test_stop_distance_hand_worked(cfg, atr, mae_pctl, expected):
    assert stop_distance(atr=atr, mae_pctl=mae_pctl, config=cfg) == pytest.approx(expected)


def test_stop_distance_never_below_one_atr(cfg):
    tight = dataclasses.replace(cfg, stop_atr_mult=0.25)
    assert stop_distance(atr=2.0, mae_pctl=0.1, config=tight) == pytest.approx(2.0)


@pytest.mark.parametrize("bad", [0.0, -0.5, float("nan")])
def test_stop_distance_rejects_nonpositive_atr(cfg, bad):
    with pytest.raises(ValueError):
        stop_distance(atr=bad, mae_pctl=1.0, config=cfg)


def test_plan_stop_rejects_nonpositive_atr(cfg):
    with pytest.raises(ValueError):
        plan_stop(
            Position("AAA", 10, 100.0, last=100.0),
            atr=0.0,
            side="long",
            deviation_bps=12.0,
            config=cfg,
            market=_market(),
        )


# --------------------------------------------------------------------------
# plan_stop: symmetry, safe-side limit, cluster handling
# --------------------------------------------------------------------------
def test_long_and_short_stops_mirror(cfg):
    reference = 100.13
    market = _market()
    long_plan = plan_stop(
        Position("AAA", 10, reference, last=reference),
        atr=2.0,
        side="long",
        deviation_bps=12.0,
        config=cfg,
        market=market,
    )
    short_plan = plan_stop(
        Position("AAA", -10, reference, last=reference),
        atr=2.0,
        side="short",
        deviation_bps=12.0,
        config=cfg,
        market=market,
    )
    assert long_plan.stop_kind == STOP_KIND == "synthetic_stop_limit"
    assert long_plan.adjusted_for_cluster is False
    assert short_plan.adjusted_for_cluster is False
    # 3.50 distance each way, mirrored about the entry mark.
    assert long_plan.trigger_price == pytest.approx(reference - 3.5)
    assert short_plan.trigger_price == pytest.approx(reference + 3.5)
    assert long_plan.limit_price + short_plan.limit_price == pytest.approx(2 * reference)
    # The limit is always on the safe side of its trigger.
    assert long_plan.limit_price < long_plan.trigger_price
    assert short_plan.limit_price > short_plan.trigger_price


@pytest.mark.parametrize("side", ["long", "short"])
def test_limit_stays_on_the_safe_side_of_the_trigger(cfg, side):
    reference = 100.0
    plan = plan_stop(
        Position("AAA", 10 if side == "long" else -10, reference, last=reference),
        atr=2.0,
        side=side,
        deviation_bps=12.0,
        config=cfg,
        market=_market(last_bar_high=None, last_bar_low=None),
    )
    if side == "long":
        assert plan.limit_price < plan.trigger_price
    else:
        assert plan.limit_price > plan.trigger_price


def test_round_number_stop_is_moved_and_flagged(cfg):
    # distance 1.75 -> raw trigger 100.00, exactly a round number.
    plan = plan_stop(
        Position("AAA", 10, 101.75, last=101.75),
        atr=1.0,
        side="long",
        deviation_bps=12.0,
        config=cfg,
        market=_market(),
    )
    assert plan.adjusted_for_cluster is True
    assert plan.trigger_price == pytest.approx(99.99)


def test_clean_stop_is_not_moved(cfg):
    plan = plan_stop(
        Position("AAA", 10, 101.73, last=101.73),
        atr=1.0,
        side="long",
        deviation_bps=12.0,
        config=cfg,
        market=_market(),
    )
    assert plan.adjusted_for_cluster is False
    assert plan.trigger_price == pytest.approx(99.98)


def test_prior_extreme_is_treated_as_a_cluster(cfg):
    market = _market(last_bar_low=99.37)
    assert cluster_clearance(99.37, market=market, config=cfg) == pytest.approx(99.36)


def test_prior_extreme_stop_is_moved_and_flagged(cfg):
    # 101.37 - 1.75 = 99.62, exactly the prior day's low.
    plan = plan_stop(
        Position("AAA", 10, 101.37, last=101.37),
        atr=1.0,
        side="long",
        deviation_bps=12.0,
        config=cfg,
        market=_market(last_bar_low=99.62),
    )
    assert plan.adjusted_for_cluster is True
    assert plan.trigger_price == pytest.approx(99.61)
    assert plan.limit_price < plan.trigger_price


def test_plan_stop_uses_last_before_avg_entry(cfg):
    plan = plan_stop(
        Position("AAA", 10, 90.0, last=101.73),
        atr=1.0,
        side="long",
        deviation_bps=12.0,
        config=cfg,
        market=_market(),
    )
    assert plan.trigger_price == pytest.approx(99.98)


# --------------------------------------------------------------------------
# protective_orders
# --------------------------------------------------------------------------
def test_protective_orders_one_leg_per_open_position(cfg):
    positions = [
        Position("AAA", 10, 100.0, sleeve=INTRADAY, stop=95.0, last=100.0),
        Position("BBB", -5, 50.0, sleeve=INTRADAY, stop=52.5, last=50.0),
    ]
    legs = protective_orders(positions, stops={}, config=cfg)
    assert [leg.symbol for leg in legs] == ["AAA", "BBB"]
    assert all(leg.order_type == "stop_limit" for leg in legs)
    assert all(leg.qty == int(abs(p.qty)) for leg, p in zip(legs, positions, strict=True))
    sell_leg, buy_leg = legs
    assert sell_leg.side == "sell"
    assert sell_leg.stop_price == pytest.approx(95.0)
    assert sell_leg.limit_price < sell_leg.stop_price
    assert sell_leg.client_order_id == client_order_id(INTRADAY, "AAA", "stop")
    assert buy_leg.side == "buy"
    assert buy_leg.stop_price == pytest.approx(52.5)
    assert buy_leg.limit_price > buy_leg.stop_price


def test_protective_orders_skips_covered_flat_and_unlevelled(cfg):
    positions = [
        Position("AAA", 10, 100.0, stop=95.0, last=100.0),  # covered below
        Position("BBB", 0, 100.0, stop=None, last=100.0),  # flat
        Position("CCC", 10, 100.0, stop=None, last=100.0),  # no level
        Position("DDD", -5, 50.0, stop=52.5, last=50.0),  # needs a leg
    ]
    legs = protective_orders(positions, stops={"AAA": 95.0}, config=cfg)
    assert [leg.symbol for leg in legs] == ["DDD"]
