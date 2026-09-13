"""The five setups: priority, regime gating, and hand-worked stop/target levels (plan §5.2)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import pytest

from signald.signals.setups import SignalCandidate, _gap_fade, eligible, scan

pytestmark = pytest.mark.timeout(120)

OPEN = datetime(2026, 9, 12, 9, 30)


@dataclass(frozen=True)
class Bar:
    """Stand-in for ``signald.marketdata.service.Bar`` (same field names)."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class DayType:
    """Stand-in for ``signald.marketdata.daytype.DayType`` (same field names)."""

    day_type: str
    adx_14: float | None = None
    vwap: float | None = None
    vwap_slope: float | None = None
    rvol_first5: float | None = None
    rvol_rank: int | None = None
    gap_pct: float | None = None
    gap_atr_bucket: str | None = None
    opening_range_high: float | None = None
    opening_range_low: float | None = None


def bar(i: int, *, o: float, h: float, lo: float, c: float, v: float = 0.0) -> Bar:
    return Bar(ts=OPEN + timedelta(minutes=5 * i), open=o, high=h, low=lo, close=c, volume=v)


def run(cfg, *, bars, day_type, first5=(), prior_close=None, atr=2.0, adv=2_000_000.0):
    return scan(
        symbol="AAPL",
        bars=bars,
        first5=first5,
        day_type=day_type,
        prior_close=prior_close,
        atr_value=atr,
        adv_shares=adv,
        baseline_shares=None,
        config=cfg,
        minutes_held=60.0,
    )


# --------------------------------------------------------------------------
# eligible (plan §5.2, design §6.1)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("price", "adv", "atr", "expected"),
    [
        (None, 2_000_000.0, 2.0, "no_price"),
        (5.0, 2_000_000.0, 2.0, "price_below_min"),
        (4.99, 2_000_000.0, 2.0, "price_below_min"),
        (5.01, None, 2.0, "no_adv"),
        (5.01, 1_000_000.0, 2.0, "adv_below_min"),
        (5.01, 2_000_000.0, None, "no_atr"),
        (5.01, 2_000_000.0, 0.5, "atr_below_min"),
    ],
)
def test_eligible_rejects_each_input_and_names_it(cfg, price, adv, atr, expected):
    ok, reason = eligible("AAPL", price=price, adv_shares=adv, atr=atr, config=cfg)
    assert (ok, reason) == (False, expected)


def test_eligible_accepts_values_just_above_every_floor(cfg):
    ok, reason = eligible("AAPL", price=5.01, adv_shares=1_000_001.0, atr=0.51, config=cfg)
    assert (ok, reason) == (True, "ok")


# --------------------------------------------------------------------------
# one happy candidate per setup, with hand-worked levels
# --------------------------------------------------------------------------
def test_orb_breakout_levels_and_stop_floor(cfg):
    dt = DayType(
        "trend", rvol_first5=3.0, rvol_rank=5, opening_range_high=100.5, opening_range_low=99.0
    )
    bars = (bar(0, o=100.0, h=100.5, lo=99.0, c=100.2), bar(1, o=100.2, h=101.2, lo=100.0, c=101.0))
    cand, reason = run(cfg, bars=bars, day_type=dt)
    assert reason == "ok"
    assert isinstance(cand, SignalCandidate)
    assert (cand.setup, cand.side) == ("ORB_RVOL", "buy")
    assert cand.entry_price == 100.5  # the opening-range high break
    assert cand.stop_distance == pytest.approx(3.5)  # max(1.75*2, 100.5-99.0)
    assert cand.stop_price == pytest.approx(97.0)
    assert cand.target_price == pytest.approx(135.5)  # 10R
    assert cand.expected_move_bps > 0.0


def test_gap_go_breaks_the_first_fifteen_minute_high(cfg):
    dt = DayType("trend", rvol_first5=1.0, gap_atr_bucket="go", opening_range_low=99.0)
    bars = (
        bar(0, o=99.0, h=100.0, lo=98.5, c=99.5),
        bar(1, o=99.5, h=100.8, lo=99.4, c=100.5),
        bar(2, o=100.5, h=100.5, lo=100.0, c=100.2),
        bar(3, o=100.2, h=102.2, lo=100.1, c=102.0),  # 09:45 is outside the 15-min window
    )
    cand, reason = run(cfg, bars=bars, day_type=dt)
    assert reason == "ok"
    assert cand.setup == "GAP_GO" and cand.side == "buy"
    assert cand.entry_price == 100.8  # first-15-min high
    assert cand.stop_price == pytest.approx(95.76)  # min(99.0-0.01, 100.8*(1-0.05))
    assert cand.stop_distance == pytest.approx(5.04)
    assert cand.target_price == pytest.approx(115.92)  # 3R


def test_gap_go_needs_the_go_bucket(cfg):
    bars = (
        bar(0, o=99.0, h=100.0, lo=98.5, c=99.5),
        bar(1, o=99.5, h=100.8, lo=99.4, c=100.5),
        bar(2, o=100.5, h=100.5, lo=100.0, c=100.2),
        bar(3, o=100.2, h=102.2, lo=100.1, c=102.0),
    )
    fade, _ = run(
        cfg, bars=bars,
        day_type=DayType("trend", rvol_first5=1.0, gap_atr_bucket="fade", opening_range_low=99.0),
    )
    assert fade is None
    go, _ = run(
        cfg, bars=bars,
        day_type=DayType("trend", rvol_first5=1.0, gap_atr_bucket="go", opening_range_low=99.0),
    )
    assert go is not None and go.setup == "GAP_GO"


def test_vwap_pullback_holds_the_sloping_vwap(cfg):
    dt = DayType("trend", rvol_first5=1.0, vwap=100.0, vwap_slope=0.02)
    bars = (bar(0, o=99.5, h=100.5, lo=99.5, c=100.0), bar(1, o=100.0, h=101.2, lo=100.4, c=101.0))
    cand, reason = run(cfg, bars=bars, day_type=dt)
    assert reason == "ok"
    assert (cand.setup, cand.side) == ("VWAP_PULLBACK", "buy")
    assert cand.entry_price == 101.0
    assert cand.stop_price == pytest.approx(97.5)  # 101.0 - 1.75*2
    assert cand.target_price is None


def test_vwap_revert_needs_a_one_atr_stretch_and_enters_at_vwap(cfg):
    bars = (bar(0, o=99.0, h=99.5, lo=97.5, c=98.0),)
    long_side, reason = run(cfg, bars=bars, day_type=DayType("range", vwap=100.0))
    assert reason == "ok"
    assert (long_side.setup, long_side.side) == ("VWAP_REVERT", "buy")
    assert long_side.entry_price == 100.0  # enters at VWAP
    assert long_side.stop_price == pytest.approx(96.5)
    assert long_side.target_price is None

    short_bars = (bar(0, o=101.0, h=102.5, lo=100.5, c=102.0),)
    short_side, _ = run(cfg, bars=short_bars, day_type=DayType("range", vwap=100.0))
    assert (short_side.setup, short_side.side) == ("VWAP_REVERT", "sell")
    assert short_side.stop_price == pytest.approx(103.5)


def test_gap_fade_stops_above_the_opening_high_and_targets_the_prior_close(cfg):
    dt = DayType(
        "range", gap_atr_bucket="fade", gap_pct=0.01,
        opening_range_high=100.5, opening_range_low=99.0,
    )
    bars = (bar(0, o=99.5, h=100.5, lo=99.0, c=100.0),)
    cand, reason = run(cfg, bars=bars, day_type=dt, prior_close=97.0)
    assert reason == "ok"
    assert (cand.setup, cand.side) == ("GAP_FADE", "sell")
    assert cand.entry_price == 100.0
    assert cand.stop_price == pytest.approx(100.51)  # opening high + one tick
    assert cand.target_price == 97.0


def test_gap_fade_below_the_open_buys_against_a_gap_down(cfg):
    dt = DayType(
        "range", gap_atr_bucket="fade", gap_pct=-0.01,
        opening_range_high=100.5, opening_range_low=99.0,
    )
    bars = (bar(0, o=99.5, h=100.5, lo=99.0, c=100.0),)
    cand, reason = run(cfg, bars=bars, day_type=dt, prior_close=103.0)
    assert reason == "ok"
    assert (cand.setup, cand.side) == ("GAP_FADE", "buy")
    assert cand.stop_price == pytest.approx(98.99)  # opening low - one tick
    assert cand.target_price == 103.0


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------
def test_orb_refuses_a_low_rvol_name(cfg):
    dt = DayType(
        "trend", rvol_first5=1.5, rvol_rank=5, opening_range_high=100.5, opening_range_low=99.0
    )
    bars = (bar(0, o=100.0, h=100.5, lo=99.0, c=100.2), bar(1, o=100.2, h=101.2, lo=100.0, c=101.0))
    cand, reason = run(cfg, bars=bars, day_type=dt)
    assert cand is None and reason == "rvol_low"


def test_orb_refuses_a_rank_outside_the_top_n(cfg):
    dt = DayType(
        "trend", rvol_first5=3.0, rvol_rank=25, opening_range_high=100.5, opening_range_low=99.0
    )
    bars = (bar(0, o=100.0, h=100.5, lo=99.0, c=100.2), bar(1, o=100.2, h=101.2, lo=100.0, c=101.0))
    cand, reason = run(cfg, bars=bars, day_type=dt)
    assert cand is None and reason == "rank_low"


def test_a_degenerate_stop_is_refused(cfg):
    # gap up, but price already broke above the opening high: the short's stop
    # (above the high) sits below the entry -> distance <= 0.
    dt = DayType(
        "range", gap_atr_bucket="fade", gap_pct=0.01,
        opening_range_high=100.5, opening_range_low=99.0,
    )
    bars = (bar(0, o=100.0, h=101.5, lo=99.5, c=101.0),)
    cand, reason = _gap_fade(
        symbol="AAPL", last=101.0, first5=(), day_type=dt, prior_close=99.0,
        atr=2.0, config=cfg, cluster=None, minutes_held=60.0,
    )
    assert cand is None and reason == "degenerate_stop"
    observed, reason = run(cfg, bars=bars, day_type=dt, prior_close=99.0)
    assert observed is None and reason


# --------------------------------------------------------------------------
# regime gating and the scan contract
# --------------------------------------------------------------------------
def test_a_trend_setup_on_a_range_day_is_skipped(cfg):
    dt = DayType(
        "range", rvol_first5=3.0, rvol_rank=5, opening_range_high=100.5, opening_range_low=99.0
    )
    bars = (bar(0, o=100.0, h=100.5, lo=99.0, c=101.0),)  # an ORB break, but range day
    cand, reason = run(cfg, bars=bars, day_type=dt)
    assert cand is None and reason == "regime_mismatch"


def test_a_reversion_setup_on_a_trend_day_is_skipped(cfg):
    bars = (bar(0, o=99.0, h=99.5, lo=97.5, c=98.0),)  # 2 ATR below vwap -> revert long
    trend = DayType("trend", rvol_first5=1.0, vwap=100.0)
    skipped, _ = run(cfg, bars=bars, day_type=trend)
    assert skipped is None
    emitted, reason = run(cfg, bars=bars, day_type=replace(trend, day_type="range"))
    assert reason == "ok" and emitted.setup == "VWAP_REVERT"


def test_day_type_may_be_a_bare_string(cfg):
    bars = (bar(0, o=100.0, h=100.5, lo=99.0, c=101.0),)
    typed = run(cfg, bars=bars, day_type=DayType("range"))
    plain = run(cfg, bars=bars, day_type="range")
    assert typed == plain == (None, "regime_mismatch")


@pytest.mark.parametrize("day", ["trend", "range", "unknown"])
def test_scan_always_returns_a_reason_when_it_returns_none(cfg, day):
    cand, reason = run(cfg, bars=(), day_type=day)
    assert cand is None
    assert isinstance(reason, str) and reason
