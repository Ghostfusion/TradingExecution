"""Day-type classifier: ATR/ADX/VWAP/RVOL and the classify decision (design §6.2).

Every formula is checked against arithmetic worked in the comment above it, and
the classifier is checked for all four gap buckets plus the trend/range/unknown
paths. Insufficient data must never be guessed: it degrades to "unknown" with a
reason. No wall clock, no network; bars are built by hand.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from signald.marketdata.daytype import (
    ADX_PERIOD,
    adx,
    atr,
    classify,
    opening_range,
    relative_volume,
    rvol_ranks,
    session_vwap,
    true_range,
    vwap_slope,
)
from signald.marketdata.service import Bar

pytestmark = pytest.mark.timeout(120)

#: 2026-09-11 is a Friday session; the calendar's open is 09:30 ET.
BASE = datetime(2026, 9, 11, 9, 30)

#: Hand series: (open, high, low, close).
RAW5 = [
    (10.0, 11.0, 9.5, 10.5),
    (10.5, 11.5, 10.0, 11.0),
    (11.0, 12.0, 10.8, 11.8),
    (11.8, 12.2, 11.0, 11.5),
    (11.5, 12.0, 11.2, 11.9),
]

#: A generated 15-bar series whose ADX(14) is ~24.41 - deliberately inside the
#: default 20..25 "no trade" band. (open, high, low, close).
IN_BETWEEN = [
    (100.0, 100.39676362463004, 99.85494789094491, 100.24807605509814),
    (100.24807605509814, 100.70575409639585, 100.05632147138026, 100.54071281327738),
    (100.54071281327738, 100.6220628747457, 98.903430324067, 99.19990194579637),
    (99.19990194579637, 100.31740944287614, 98.93479348331998, 100.09380817337801),
    (100.09380817337801, 101.28404050598962, 99.98467376087072, 100.98505439932072),
    (100.98505439932072, 101.26765062239016, 100.01241600796193, 100.14266527779378),
    (100.14266527779378, 100.26453058289967, 98.9726797649624, 99.23753114889745),
    (99.23753114889745, 100.82184329933334, 99.19478210689161, 100.80633467840462),
    (100.80633467840462, 101.18674687856954, 100.65499575951378, 101.0754937911892),
    (101.0754937911892, 101.12702938176074, 100.29426938340532, 100.3143984056507),
    (100.3143984056507, 101.63642956216952, 100.255195894113, 101.34636674020452),
    (101.34636674020452, 101.62020632974682, 99.5611535967675, 99.65551138768474),
    (99.65551138768474, 100.12593576832533, 99.38817019674994, 99.91947079860563),
    (99.91947079860563, 99.97830468192659, 98.49758337011772, 98.63946993189062),
    (98.63946993189062, 98.6864136017691, 98.35245373387814, 98.4602160343763),
]


def _bar(symbol, minute, o, h, low, c, volume=1000.0):
    return Bar(symbol, BASE + timedelta(minutes=minute), o, h, low, c, volume)


def _trend_bars(symbol="AA", count=15):
    # Monotonic uptrend: +DM = 1 every bar, -DM = 0, close = open.
    return [_bar(symbol, i, 10 + i, 10.5 + i, 9.5 + i, 10 + i) for i in range(count)]


def _flat_bars(symbol="RR", count=15):
    return [_bar(symbol, i, 10.0, 10.0, 10.0, 10.0) for i in range(count)]


# --- true range / ATR -----------------------------------------------------
def test_true_range_hand_worked():
    bars = [_bar("T", i, *row) for i, row in enumerate(RAW5)]
    # TR_0 = high - low = 1.5 (no prior close);
    # TR_1 = max(11.5-10.0, |11.5-10.5|, |10.0-10.5|) = max(1.5, 1.0, 0.5) = 1.5
    # TR_2 = max(12.0-10.8, |12.0-11.0|, |10.8-11.0|) = max(1.2, 1.0, 0.2) = 1.2
    # TR_3 = max(12.2-11.0, |12.2-11.8|, |11.0-11.8|) = max(1.2, 0.4, 0.8) = 1.2
    # TR_4 = max(12.0-11.2, |12.0-11.5|, |11.2-11.5|) = max(0.8, 0.5, 0.3) = 0.8
    expected = [1.5, 1.5, 1.2, 1.2, 0.8]
    got = [true_range(bars[i], bars[i - 1].close if i else None) for i in range(len(bars))]
    assert got == pytest.approx(expected)


def test_atr_seeds_with_the_mean_true_range():
    bars = [_bar("T", i, *row) for i, row in enumerate(RAW5)]
    # period 4 on 5 bars: TRs = [1.5, 1.2, 1.2, 0.8] (uses prior closes),
    # ATR = (1.5 + 1.2 + 1.2 + 0.8) / 4 = 4.7 / 4 = 1.175
    assert atr(bars, period=4) == pytest.approx(1.175)


def test_atr_requires_period_plus_one_bars():
    bars = [_bar("T", i, *row) for i, row in enumerate(RAW5)]
    with pytest.raises(ValueError):
        atr(bars[:4], period=4)


# --- ADX ------------------------------------------------------------------
def test_adx_flags_a_monotonic_uptrend():
    bars = _trend_bars()
    # Every bar adds +DM=1 with no -DM, so DX=100 on the seed and ADX=100.
    assert adx(bars) == pytest.approx(100.0)
    assert adx(bars) > 25


def test_adx_is_zero_on_a_flat_series():
    bars = _flat_bars()
    # No movement: true range sums to 0, so the directional index is 0.
    assert adx(bars) == pytest.approx(0.0)
    assert adx(bars) < 20


# --- VWAP -----------------------------------------------------------------
def test_session_vwap_hand_worked():
    bars = [
        _bar("V", 0, 10.0, 11.0, 9.0, 10.0, volume=100.0),  # typical 10.0
        _bar("V", 1, 12.0, 13.0, 11.0, 12.0, volume=300.0),  # typical 12.0
    ]
    # (10.0*100 + 12.0*300) / 400 = 4600 / 400 = 11.5
    assert session_vwap(bars) == pytest.approx(11.5)


def test_session_vwap_needs_volume():
    with pytest.raises(ValueError):
        session_vwap([_bar("V", 0, 10.0, 11.0, 9.0, 10.0, volume=0.0)])


def test_vwap_slope_hand_worked():
    bars = [_bar("V", i, 10.0, 11.0, 9.0, 10.0, volume=100.0) for i in range(5)]
    bars.append(_bar("V", 5, 20.0, 21.0, 19.0, 20.0, volume=100.0))  # typical 20.0
    # cumulative VWAP: 10.0 five times, then (5000 + 2000) / 600 = 11.6667
    # slope over lookback 5 = (11.6667 - 10.0) / 5 = 0.33333
    assert vwap_slope(bars, lookback=5) == pytest.approx(0.3333333, abs=1e-6)


# --- opening range --------------------------------------------------------
def test_opening_range_takes_only_the_first_n_minutes():
    bars = [_bar("O", i, 10.0 + i, 10.5 + i, 9.5 + i, 10.0 + i) for i in range(10)]
    # window [09:30, 09:35) = minutes 0..4: high = 14.5, low = 9.5
    window = opening_range(bars, minutes=5)
    assert window is not None
    assert window[0] == pytest.approx(14.5)
    assert window[1] == pytest.approx(9.5)


def test_opening_range_honours_an_explicit_session_open():
    bars = [_bar("O", i, 10.0 + i, 10.5 + i, 9.5 + i, 10.0 + i) for i in range(10)]
    # window [09:32, 09:34) = minutes 2..3: high = 13.5, low = 11.5
    window = opening_range(bars, minutes=2, session_open=time(9, 32))
    assert window is not None
    assert window[0] == pytest.approx(13.5)
    assert window[1] == pytest.approx(11.5)


def test_opening_range_is_none_without_bars_in_the_window():
    bars = [_bar("O", 10 + i, 10.0 + i, 10.5 + i, 9.5 + i, 10.0 + i) for i in range(3)]
    assert opening_range(bars, minutes=5) is None


# --- RVOL -----------------------------------------------------------------
def test_relative_volume_hand_worked():
    first5 = [_bar("R", i, 10.0, 10.0, 10.0, 10.0, volume=2000.0) for i in range(5)]
    # 10000 shares / (78000 / 78) = 10000 / 1000 = 10.0
    assert relative_volume(first5, baseline_shares=78000.0) == pytest.approx(10.0)


@pytest.mark.parametrize("baseline", [None, 0.0, -5.0])
def test_relative_volume_is_none_without_a_baseline(baseline):
    first5 = [_bar("R", i, 10.0, 10.0, 10.0, 10.0, volume=2000.0) for i in range(5)]
    assert relative_volume(first5, baseline_shares=baseline) is None


def test_rvol_ranks_highest_first_and_ties_share_the_lower_rank():
    ranks = rvol_ranks({"A": 3.0, "B": 1.0, "C": 3.0})
    assert ranks == {"A": 1, "C": 1, "B": 3}


# --- classify -------------------------------------------------------------
def test_classify_trend_path(cfg):
    bars = _trend_bars("AA")
    result = classify(
        symbol="AA",
        bars=bars,
        first5=bars[:5],
        prior_close=9.5,
        baseline_shares=78000.0,
        rvols={"AA": 1},
        config=cfg,
    )
    assert result.day_type == "trend"
    assert result.adx_14 == pytest.approx(100.0)
    # typical = 10 + i, equal volumes -> VWAP = 17.0; slope = (17.0 - 14.5) / 5
    assert result.vwap == pytest.approx(17.0)
    assert result.vwap_slope == pytest.approx(0.5)
    assert result.rvol_first5 == pytest.approx(5.0)
    assert result.rvol_rank == 1
    assert result.gap_atr_bucket == "go"  # gap 0.5 / ATR 1.5, +5.3% and RVOL 5
    assert result.opening_range_high == pytest.approx(14.5)
    assert result.opening_range_low == pytest.approx(9.5)


def test_classify_range_path_and_dead_atr(cfg):
    bars = _flat_bars("RR")
    result = classify(
        symbol="RR",
        bars=bars,
        first5=bars[:5],
        prior_close=10.0,
        baseline_shares=78000.0,
        rvols={"RR": 2},
        config=cfg,
    )
    assert result.day_type == "range"
    assert result.adx_14 == pytest.approx(0.0)
    assert result.rvol_rank == 2
    # A flat tape has zero ATR, so the gap cannot be measured in ATR units.
    assert result.gap_atr_bucket is None
    assert "gap_inputs_missing" in result.reasons


def test_classify_unknown_when_adx_sits_between_the_thresholds(cfg):
    bars = [_bar("XX", i, *row) for i, row in enumerate(IN_BETWEEN)]
    result = classify(
        symbol="XX",
        bars=bars,
        first5=bars[:5],
        prior_close=bars[0].open,
        baseline_shares=78000.0,
        rvols={"XX": 1},
        config=cfg,
    )
    assert result.day_type == "unknown"
    assert cfg.adx_range < result.adx_14 < cfg.adx_trend
    assert "adx_between_thresholds" in result.reasons


def test_classify_insufficient_bars_is_unknown_with_a_reason(cfg):
    bars = _trend_bars("AA", count=ADX_PERIOD)  # 14 < ADX_PERIOD + 1
    result = classify(
        symbol="AA",
        bars=bars,
        first5=bars[:5],
        prior_close=9.5,
        baseline_shares=78000.0,
        rvols={"AA": 1},
        config=cfg,
    )
    assert result.day_type == "unknown"
    assert result.reasons == ("insufficient_bars",)
    assert result.adx_14 is None
    assert result.vwap is None
    assert result.opening_range_high is None


@pytest.mark.parametrize(
    "prior_close,baseline,expected",
    [
        (9.5, 78000.0, "go"),       # gap +0.5 (5.3%), ATR 1.5, RVOL 5.0
        (8.0, 780000.0, "too_big"), # |gap| 2.0 = 1.33x ATR, RVOL 0.5
        (9.8, 780000.0, "fade"),    # |gap| 0.2 = 0.13x ATR
        (9.5, 780000.0, "none"),    # |gap| 0.5 = 0.33x ATR, RVOL below 3
    ],
)
def test_classify_gap_buckets(cfg, prior_close, baseline, expected):
    bars = _trend_bars("AA")
    result = classify(
        symbol="AA",
        bars=bars,
        first5=bars[:5],
        prior_close=prior_close,
        baseline_shares=baseline,
        rvols={"AA": 1},
        config=cfg,
    )
    assert result.gap_atr_bucket == expected


def test_classify_reasons_cover_every_missing_input(cfg):
    bars = _trend_bars("AA")
    result = classify(
        symbol="AA",
        bars=bars,
        first5=bars[:5],
        prior_close=None,
        baseline_shares=None,
        rvols=None,
        config=cfg,
    )
    assert result.rvol_first5 is None
    assert "rvol_baseline_missing" in result.reasons
    assert result.rvol_rank is None
    assert "rvol_rank_missing" in result.reasons
    assert result.gap_pct is None
    assert "prior_close_missing" in result.reasons
    assert result.gap_atr_bucket is None
    assert "gap_inputs_missing" in result.reasons
