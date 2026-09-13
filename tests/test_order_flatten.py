"""EOD flatten: cutoff countdown, flat check, auction route, verification.

The closing-auction cutoff is ``config.intraday_flat_by`` (15:50 default): a
plan made at 15:45 routes ``tif=cls``; one made after 15:50 routes marketable
limits. ``verify_flat`` is the only function that may declare the book flat.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from signald.order.flatten import (
    flatten_plan,
    is_flat,
    seconds_to_flat,
    verify_flat,
)
from signald.risk.state import Position

pytestmark = pytest.mark.timeout(120)

BEFORE = datetime(2026, 9, 12, 15, 45, 0)
AFTER = datetime(2026, 9, 12, 15, 55, 0)


class _EtClock:
    """Calendar stub: the injected stamp already is the ET clock."""

    @staticmethod
    def to_et(stamp):
        return stamp


# --------------------------------------------------------------------------
# seconds_to_flat
# --------------------------------------------------------------------------
def test_seconds_to_flat_before_cutoff_is_positive(cfg):
    assert seconds_to_flat(BEFORE, config=cfg, calendar=_EtClock()) == pytest.approx(300.0)


def test_seconds_to_flat_after_cutoff_is_negative(cfg):
    assert seconds_to_flat(AFTER, config=cfg, calendar=_EtClock()) == pytest.approx(-300.0)


def test_seconds_to_flat_treats_stamp_as_et_without_a_converter(cfg):
    class NoConversion:
        pass

    now = datetime(2026, 9, 12, 15, 49, 0)
    assert seconds_to_flat(now, config=cfg, calendar=NoConversion()) == pytest.approx(60.0)


# --------------------------------------------------------------------------
# is_flat
# --------------------------------------------------------------------------
def test_is_flat_empty_book():
    assert is_flat([]) == (True, ())


def test_is_flat_allows_dust():
    assert is_flat([Position("AAA", 0.5, 10.0)]) == (True, ())


def test_is_flat_reports_residual_symbols():
    flat, residual = is_flat(
        [Position("BBB", 0.5, 10.0), Position("AAA", 12.0, 10.0)]
    )
    assert flat is False
    assert residual == ("AAA",)


# --------------------------------------------------------------------------
# flatten_plan
# --------------------------------------------------------------------------
def test_flatten_plan_uses_closing_auction_before_cutoff(cfg):
    plan = flatten_plan(
        [Position("AAA", 10, 100.0, last=101.0)],
        now=BEFORE,
        config=cfg,
        calendar=_EtClock(),
    )
    assert plan.use_closing_auction is True
    leg = plan.orders[0]
    assert leg.order_type == "marketable_limit"
    assert leg.tif == "cls"
    assert leg.side == "sell"
    assert leg.limit_price < 101.0


def test_flatten_plan_uses_marketable_limit_after_cutoff(cfg):
    plan = flatten_plan(
        [Position("AAA", 10, 100.0, last=101.0)],
        now=AFTER,
        config=cfg,
        calendar=_EtClock(),
    )
    assert plan.use_closing_auction is False
    leg = plan.orders[0]
    assert leg.order_type == "marketable_limit"
    assert leg.tif == "day"
    assert leg.limit_price < 101.0


def test_flatten_plan_marketable_when_auction_not_eligible(cfg):
    plan = flatten_plan(
        [Position("AAA", 10, 100.0, last=101.0)],
        now=BEFORE,
        config=cfg,
        calendar=_EtClock(),
        closing_auction_eligible=False,
    )
    assert plan.use_closing_auction is False
    assert plan.orders[0].tif == "day"


def test_flatten_plan_covers_a_short_on_the_buy_side(cfg):
    plan = flatten_plan(
        [Position("AAA", -10, 100.0, last=101.0)],
        now=BEFORE,
        config=cfg,
        calendar=_EtClock(),
    )
    leg = plan.orders[0]
    assert leg.side == "buy"
    assert leg.limit_price > 101.0


def test_flatten_plan_already_flat_has_no_orders(cfg):
    plan = flatten_plan(
        [Position("AAA", 0.5, 100.0, last=101.0)],
        now=BEFORE,
        config=cfg,
        calendar=_EtClock(),
    )
    assert plan.orders == ()
    assert plan.use_closing_auction is False


def test_flatten_plan_does_not_drop_a_priceless_position(cfg):
    plan = flatten_plan(
        [Position("AAA", 10, 100.0, last=None)],
        now=BEFORE,
        config=cfg,
        calendar=_EtClock(),
    )
    assert len(plan.orders) == 1
    leg = plan.orders[0]
    assert leg.order_type == "needs_manual"
    assert leg.limit_price is None
    assert leg.stop_price is None
    assert leg.symbol == "AAA"
    assert "needs_manual" in plan.reason
    assert "AAA" in plan.reason


# --------------------------------------------------------------------------
# verify_flat
# --------------------------------------------------------------------------
def test_verify_flat_returns_false_until_the_broker_is_empty(cfg):
    state = {"positions": [Position("AAA", 10, 100.0, last=100.0)]}

    def broker():
        return state["positions"]

    assert verify_flat(broker, now=AFTER, config=cfg) == (False, "residual: AAA")
    state["positions"] = []
    assert verify_flat(broker, now=AFTER, config=cfg) == (True, "flat")


def test_verify_flat_unavailable_is_not_flat(cfg):
    assert verify_flat(lambda: None, now=AFTER, config=cfg) == (
        False,
        "broker positions unavailable",
    )


def test_verify_flat_rereads_on_every_attempt(cfg):
    calls = {"n": 0}

    def broker():
        calls["n"] += 1
        if calls["n"] >= 2:
            return []
        return [Position("AAA", 10, 100.0, last=100.0)]

    assert verify_flat(broker, now=AFTER, config=cfg, attempts=2) == (True, "flat")
    assert calls["n"] == 2


def test_verify_flat_rejects_zero_attempts(cfg):
    with pytest.raises(ValueError):
        verify_flat(lambda: [], now=AFTER, config=cfg, attempts=0)
