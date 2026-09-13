"""Day-type classifier: the gate that comes before any intraday setup.

Invariant (design §6.2, plan §5.1): a setup is only ever offered a day type the
data can actually support. Fewer bars than ADX(14) needs, a missing prior close
or a missing RVOL baseline each degrade to ``day_type="unknown"`` with a reason
in ``reasons`` - never a guess, never a fabricated number. ATR, ADX, VWAP and
RVOL are computed here and only here; the setups import them from this module.

The calendar is imported behind a guard so this module can be exercised before
the sibling ``calendar.py`` lands; without it, the opening range falls back to
the first bar's clock minute.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from ..config import Config
from .service import Bar

try:  # sibling task ships signald/marketdata/calendar.py
    from .calendar import session_for, to_et
except ImportError:  # pragma: no cover - pre-calendar checkout
    session_for = None  # type: ignore[assignment]
    to_et = None  # type: ignore[assignment]

#: Wilder's period for ATR/ADX (design §6.2).
ADX_PERIOD = 14
#: RVOL compares the first five minutes with 1/78th of a daily baseline: the
#: 6.5-hour regular session is 78 five-minute buckets.
FIVE_MIN_BUCKETS = 78

#: Gap buckets (design §6.2): a tiny gap is a fade candidate, a large one is
#: not fadeable, and a >=4% status-quo gap with RVOL >= 3 is a momentum "go".
GAP_FADE_MAX_ATR = 0.3
GAP_TOO_BIG_ATR = 1.2
GAP_GO_PCT = 0.04
GAP_GO_RVOL = 3.0


@dataclass(frozen=True)
class DayType:
    """One symbol's classified session. Every ``None`` has a reason in ``reasons``."""

    day_type: str  # "trend" | "range" | "unknown"
    adx_14: float | None
    vwap: float | None
    vwap_slope: float | None
    rvol_first5: float | None
    rvol_rank: int | None
    gap_pct: float | None
    gap_atr_bucket: str | None  # "fade" | "go" | "too_big" | "none"
    opening_range_high: float | None
    opening_range_low: float | None
    reasons: tuple[str, ...]


def true_range(bar: Bar, prev_close: float | None) -> float:
    """Wilder true range; without a prior close the bar's own range is used."""
    if prev_close is None:
        return bar.high - bar.low
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def atr(bars: Sequence[Bar], *, period: int = ADX_PERIOD) -> float:
    """Wilder ATR over ``bars`` (needs ``period + 1`` bars to seed the smoothing)."""
    if len(bars) < period + 1:
        raise ValueError(f"atr needs {period + 1} bars, got {len(bars)}")
    trs = [true_range(bars[i], bars[i - 1].close) for i in range(1, len(bars))]
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def _dx(plus_sum: float, minus_sum: float, tr_sum: float) -> float:
    """Wilder directional index in percent; an immobile window is 0, never NaN."""
    if tr_sum <= 0:
        return 0.0
    plus_di = 100.0 * plus_sum / tr_sum
    minus_di = 100.0 * minus_sum / tr_sum
    denom = plus_di + minus_di
    if denom <= 0:
        return 0.0
    return 100.0 * abs(plus_di - minus_di) / denom


def adx(bars: Sequence[Bar], *, period: int = ADX_PERIOD) -> float:
    """Wilder ADX over ``bars`` (needs ``period + 1`` bars to produce a DX)."""
    if len(bars) < period + 1:
        raise ValueError(f"adx needs {period + 1} bars, got {len(bars)}")
    plus: list[float] = []
    minus: list[float] = []
    trs: list[float] = []
    for i in range(1, len(bars)):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        trs.append(true_range(bars[i], bars[i - 1].close))
    plus_sum = sum(plus[:period]) / period
    minus_sum = sum(minus[:period]) / period
    tr_sum = sum(trs[:period]) / period
    dxs = [_dx(plus_sum, minus_sum, tr_sum)]
    for i in range(period, len(trs)):
        plus_sum = (plus_sum * (period - 1) + plus[i]) / period
        minus_sum = (minus_sum * (period - 1) + minus[i]) / period
        tr_sum = (tr_sum * (period - 1) + trs[i]) / period
        dxs.append(_dx(plus_sum, minus_sum, tr_sum))
    if len(dxs) <= period:
        return sum(dxs) / len(dxs)
    value = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        value = (value * (period - 1) + dx) / period
    return value


def session_vwap(bars: Sequence[Bar]) -> float:
    """Volume-weighted average of ``typical`` over the session; needs volume."""
    total_volume = sum(bar.volume for bar in bars)
    if not bars or total_volume <= 0:
        raise ValueError("vwap is undefined without volume")
    return sum(bar.typical * bar.volume for bar in bars) / total_volume


def vwap_slope(bars: Sequence[Bar], *, lookback: int = 5) -> float:
    """Per-bar change of the cumulative VWAP over the last ``lookback`` bars."""
    if lookback <= 0:
        raise ValueError(f"lookback must be positive, got {lookback}")
    if len(bars) <= lookback:
        raise ValueError(f"vwap_slope needs more than {lookback} bars, got {len(bars)}")
    cumulative: list[float | None] = []
    value = 0.0
    volume = 0.0
    latest: float | None = None
    for bar in bars:
        value += bar.typical * bar.volume
        volume += bar.volume
        if volume > 0:
            latest = value / volume
        cumulative.append(latest)
    start = cumulative[-1 - lookback]
    end = cumulative[-1]
    if start is None or end is None:
        raise ValueError("vwap_slope is undefined without volume")
    return (end - start) / lookback


def _session_open_dt(first: datetime, session_open: time | None) -> datetime:
    """Origin of the opening range: the calendar's open when known, else the bar."""
    if session_open is not None:
        return first.replace(
            hour=session_open.hour, minute=session_open.minute, second=0, microsecond=0
        )
    if session_for is not None and to_et is not None:
        et = to_et(first)
        if et.date() == first.date():
            session = session_for(et.date())
            if session.is_session and session.open_et is not None:
                return first.replace(
                    hour=session.open_et.hour,
                    minute=session.open_et.minute,
                    second=0,
                    microsecond=0,
                )
    return first.replace(second=0, microsecond=0)


def opening_range(
    bars: Sequence[Bar],
    *,
    minutes: int,
    session_open: time | None = None,
) -> tuple[float, float] | None:
    """(high, low) of the first ``minutes`` of the session, or ``None`` if empty."""
    if not bars or minutes <= 0:
        return None
    ordered = sorted(bars, key=lambda bar: bar.ts)
    open_dt = _session_open_dt(ordered[0].ts, session_open)
    end = open_dt + timedelta(minutes=minutes)
    window = [bar for bar in ordered if open_dt <= bar.ts < end]
    if not window:
        return None
    return (max(bar.high for bar in window), min(bar.low for bar in window))


def relative_volume(first5: Sequence[Bar], *, baseline_shares: float | None) -> float | None:
    """First-5-min volume over 1/78th of the daily baseline; ``None`` without one."""
    if baseline_shares is None or baseline_shares <= 0:
        return None
    traded = sum(bar.volume for bar in first5)
    return traded / (baseline_shares / FIVE_MIN_BUCKETS)


def rvol_ranks(rvols: Mapping[str, float]) -> dict[str, int]:
    """Competition ranks, 1 = highest; ties share the lower (better) rank."""
    return {
        symbol: 1 + sum(1 for other in rvols.values() if other > value)
        for symbol, value in sorted(rvols.items())
    }


def classify(
    *,
    symbol: str,
    bars: Sequence[Bar],
    first5: Sequence[Bar],
    prior_close: float | None,
    baseline_shares: float | None,
    rvols: Mapping[str, float] | None = None,
    config: Config,
) -> DayType:
    """Classify one symbol's session; insufficient data is ``unknown`` with a reason."""
    if len(bars) < ADX_PERIOD + 1:
        return DayType(
            day_type="unknown",
            adx_14=None,
            vwap=None,
            vwap_slope=None,
            rvol_first5=None,
            rvol_rank=None,
            gap_pct=None,
            gap_atr_bucket=None,
            opening_range_high=None,
            opening_range_low=None,
            reasons=("insufficient_bars",),
        )

    reasons: list[str] = []

    adx_14 = adx(bars, period=ADX_PERIOD)
    if adx_14 > config.adx_trend:
        day_type = "trend"
    elif adx_14 < config.adx_range:
        day_type = "range"
    else:
        day_type = "unknown"
        reasons.append("adx_between_thresholds")

    try:
        vwap = session_vwap(bars)
        slope = vwap_slope(bars)
    except ValueError:
        vwap = None
        slope = None
        reasons.append("vwap_unavailable")

    rvol_first5 = relative_volume(first5, baseline_shares=baseline_shares)
    if rvol_first5 is None:
        reasons.append("rvol_baseline_missing")

    rvol_rank = rvols.get(symbol) if rvols else None
    if rvol_rank is None:
        reasons.append("rvol_rank_missing")

    atr_14 = atr(bars, period=ADX_PERIOD)
    if prior_close is None or prior_close <= 0:
        gap_pct = None
        reasons.append("prior_close_missing")
    else:
        gap_pct = (bars[0].open - prior_close) / prior_close

    gap_atr_bucket: str | None = None
    if gap_pct is None or atr_14 <= 0:
        reasons.append("gap_inputs_missing")
    else:
        gap_ratio = abs(bars[0].open - prior_close) / atr_14
        if gap_ratio < GAP_FADE_MAX_ATR:
            gap_atr_bucket = "fade"
        elif abs(gap_pct) >= GAP_GO_PCT and rvol_first5 is not None and rvol_first5 >= GAP_GO_RVOL:
            gap_atr_bucket = "go"
        elif gap_ratio > GAP_TOO_BIG_ATR:
            gap_atr_bucket = "too_big"
        else:
            gap_atr_bucket = "none"

    window = opening_range(bars, minutes=config.opening_range_minutes)
    if window is None:
        opening_range_high = None
        opening_range_low = None
        reasons.append("opening_range_missing")
    else:
        opening_range_high, opening_range_low = window

    return DayType(
        day_type=day_type,
        adx_14=adx_14,
        vwap=vwap,
        vwap_slope=slope,
        rvol_first5=rvol_first5,
        rvol_rank=rvol_rank,
        gap_pct=gap_pct,
        gap_atr_bucket=gap_atr_bucket,
        opening_range_high=opening_range_high,
        opening_range_low=opening_range_low,
        reasons=tuple(reasons),
    )
