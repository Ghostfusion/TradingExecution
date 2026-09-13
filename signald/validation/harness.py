"""Backtest-with-costs harness: replay bars through the *live* gate/sizer (plan §P4).

The harness owns no risk rule. It iterates a point-in-time bar set in time
order, builds the ``MarketState``/``RiskRequest`` from the bars available *up to
and including* the decision bar, and delegates every decision to the caller's
``gate_factory`` (the live ``signald.risk.gate.evaluate``) and to the single
sizer ``signald.risk.sizing.size``. A refusal is recorded with a machine reason;
an allowed trade is entered at the **next** bar's open, never at the signal
bar's close, so the replay cannot read a later ``as_of`` ("no lookahead",
design §1.2).

Invariants:

* **No lookahead.** A decision at bar ``i`` reads only ``bars[0..i]``; the fill
  is bar ``i+1``'s open. A signal on the final bar is refused (``no_next_bar``).
* **Honest degradation.** An impact estimate that cannot be computed (module
  absent, missing ``sigma_daily``/ADV) falls back to ``costs.spread_bps`` only
  and is reported in ``skipped`` as ``impact_unavailable``; no number is
  fabricated. A metric with no data in :func:`summarize` is ``None`` with a
  ``<key>_reason`` sibling, never ``0``.
* **Determinism.** Timestamps come from the bars; there is no RNG and no clock.
  The timeline is the sorted union of bar timestamps and, within a timestamp,
  symbols are visited in sorted order.
* **One computation.** Gate, sizer, ES and the trade-row schema are imported,
  never re-implemented; ``trade_row`` is the only producer of the plan §10.2
  comparison unit.

Bar contract (mapping per bar, sorted by ``ts`` per symbol)::

    {ts: datetime, open: float, high: float, low: float, close: float,
     volume: float | None, signal: bool, setup: str, stop: float, target: float | None,
     side: "buy" | "sell", adv_shares: float | None, sigma_daily: float | None,
     rvol_first5, adx_14, gap_pct, spread_bps, data_vintage}

``gate_factory`` receives a :class:`HarnessContext` (the harness-owned pieces)
and returns a ``signald.risk.gate.GateDecision``; ``sizing_caps_factory`` returns
``signald.risk.sizing.SizingCaps`` for the same context. Both are closures the
caller wires to the live gate/sleeve, which keeps this module free of any second
implementation of a risk rule.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Any

from ..config import Config, parse_hhmm
from ..risk.gate import GateDecision
from ..risk.sizing import size
from ..risk.state import INTRADAY, SWING, BookState, MarketState, Position, RiskRequest
from ..risk.tail import es_historical

__all__ = [
    "HarnessContext",
    "HarnessCosts",
    "HarnessResult",
    "replay",
    "summarize",
    "trade_row",
]

#: A replay needs a decision bar and the next bar to fill at.
MIN_BARS = 2

#: The summary metrics this module reports (plan §10.1); a missing one is None.
_SUMMARY_KEYS = (
    "net_pnl_usd",
    "net_return",
    "max_drawdown_usd",
    "max_drawdown_pct",
    "return_max_dd",
    "sharpe_daily",
    "profit_factor",
    "win_rate",
    "avg_win_usd",
    "avg_loss_usd",
    "expectancy_per_trade_r",
    "expectancy_per_trade_usd",
    "turnover",
    "round_trips",
    "fees_usd",
    "slippage_bps_mean",
    "es_cvar_975_usd",
    "exposure_pct",
)

#: The exact plan §10.2 trade-row key set (the comparison's unit).
TRADE_ROW_KEYS = (
    "sleeve",
    "symbol",
    "setup",
    "entry_ts",
    "exit_ts",
    "qty",
    "entry_px",
    "exit_px",
    "slippage_bps",
    "fees_usd",
    "r_multiple",
    "rvol_first5",
    "adx_14",
    "gap_pct",
    "spread_bps",
    "exit_reason",
    "gate_verdict",
    "binding_gate",
    "data_vintage",
)


@dataclass(frozen=True)
class HarnessCosts:
    """The cost model for one replay (plan §10.3: identical for both sleeves)."""

    spread_bps: float
    impact_y: float = 0.75
    fees_bps: float = 0.0
    borrow_bps_per_day: float = 0.0


@dataclass(frozen=True)
class HarnessContext:
    """The harness-owned decision inputs handed to the caller's factories.

    ``gate_factory``/``sizing_caps_factory`` receive this and close over the
    mandate, ladder, vol and ES records the live gate also needs; the harness
    itself fabricates none of them.
    """

    symbol: str
    sleeve: str
    setup: str
    side: str
    ts: datetime
    bar: Mapping[str, Any]
    bars: tuple[Mapping[str, Any], ...]
    market: MarketState
    request: RiskRequest
    book: BookState
    config: Config


@dataclass(frozen=True)
class HarnessResult:
    """A deterministic replay output.

    ``skipped`` rows always carry ``symbol``/``reason``/``ts`` and additionally
    ``binding_gate`` (the gate that refused, or ``None`` for a non-gate skip) so
    a refusal can never be dropped silently.
    """

    trades: tuple[dict[str, Any], ...]
    skipped: tuple[dict[str, Any], ...]
    equity_curve: tuple[float, ...]
    starting_equity: float


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def trade_row(
    *,
    sleeve: str,
    symbol: str,
    setup: str,
    entry_ts: Any,
    exit_ts: Any,
    qty: float,
    entry_px: float,
    exit_px: float,
    slippage_bps: float,
    fees_usd: float,
    r_multiple: float,
    rvol_first5: float | None = None,
    adx_14: float | None = None,
    gap_pct: float | None = None,
    spread_bps: float | None = None,
    exit_reason: str = "",
    gate_verdict: str | None = None,
    binding_gate: str | None = None,
    data_vintage: str | None = None,
) -> dict[str, Any]:
    """The plan §10.2 comparison unit: exactly :data:`TRADE_ROW_KEYS`."""
    return {
        "sleeve": sleeve,
        "symbol": symbol,
        "setup": setup,
        "entry_ts": _iso(entry_ts),
        "exit_ts": _iso(exit_ts),
        "qty": float(qty),
        "entry_px": float(entry_px),
        "exit_px": float(exit_px),
        "slippage_bps": float(slippage_bps),
        "fees_usd": float(fees_usd),
        "r_multiple": float(r_multiple),
        "rvol_first5": rvol_first5,
        "adx_14": adx_14,
        "gap_pct": gap_pct,
        "spread_bps": spread_bps,
        "exit_reason": exit_reason,
        "gate_verdict": gate_verdict,
        "binding_gate": binding_gate,
        "data_vintage": data_vintage,
    }


def _as_time(value: Any) -> time | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    return parse_hhmm(str(value))


def _skip(symbol: str, reason: str, ts: Any, binding_gate: str | None = None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "reason": reason,
        "ts": _iso(ts),
        "binding_gate": binding_gate,
    }


def _impact_bps(
    *, notional_usd: float, adv_usd: float, sigma_daily: float | None, y: float
) -> float | None:
    """The square-root impact in bps, or ``None`` when it cannot be computed.

    The import is lazy (plan §P4): if ``signald.signals.costgate`` is absent the
    caller degrades to spread-only costs and records ``impact_unavailable``.
    """
    if sigma_daily is None:
        return None
    try:
        from ..signals.costgate import impact_bps
    except ImportError:
        return None
    try:
        value = impact_bps(
            notional_usd=float(notional_usd),
            adv_usd=float(adv_usd),
            sigma_daily=float(sigma_daily),
            y=float(y),
        )
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if value is None or not math.isfinite(float(value)):
        return None
    return float(value)


def _exit_for(
    spec: Mapping[str, Any], bar: Mapping[str, Any], *, is_last: bool, flat_time: time | None
) -> tuple[float, str] | None:
    """The protective/target/time exit hit on ``bar`` (stop wins ties)."""
    stop = spec["stop"]
    target = spec["target"]
    open_px = float(bar["open"])
    high = float(bar["high"])
    low = float(bar["low"])
    close = float(bar["close"])
    if spec["side"] == "buy":
        if stop is not None and low <= stop:
            return (stop if open_px > stop else open_px, "stop")
        if target is not None and high >= target:
            return (open_px if open_px >= target else target, "target")
    else:
        if stop is not None and high >= stop:
            return (stop if open_px < stop else open_px, "stop")
        if target is not None and low <= target:
            return (open_px if open_px <= target else target, "target")
    if flat_time is not None and bar["ts"].time() >= flat_time:
        return (close, "flat_by")
    if is_last:
        return (close, "eod")
    return None


def _row_pnl_usd(row: Mapping[str, Any]) -> float:
    """USD PnL reconstructed from a trade row (long-only; see module docstring).

    ``slippage_bps`` is the mean per-leg slippage, so it is applied to both legs
    at the entry notional; ``fees_usd`` is added outright.
    """
    qty = float(row["qty"])
    entry = float(row["entry_px"])
    exit_px = float(row["exit_px"])
    gross = qty * (exit_px - entry)
    round_trip_slippage = abs(qty) * entry * float(row["slippage_bps"]) / 10_000.0 * 2.0
    return gross - float(row["fees_usd"]) - round_trip_slippage


def _requested_risk_pct(config: Config, sleeve: str) -> float:
    if sleeve == INTRADAY:
        return float(config.risk_per_trade_intraday_pct)
    return float(config.risk_per_trade_swing_pct)


def _market(
    symbol: str, bar: Mapping[str, Any], *, config: Config, spread_bps: float
) -> MarketState:
    return MarketState(
        symbol=symbol,
        last=float(bar["close"]),
        spread_bps=spread_bps,
        spread_median_bps=float(config.cost_liquid_bps),
        as_of=bar["ts"],
        feed=str(config.data_feed),
        adv_shares=bar.get("adv_shares"),
        atr=bar.get("atr"),
        data_quality=str(bar.get("data_quality", "unknown")),
        price_caliber=bar.get("price_caliber"),
        last_bar_high=float(bar["high"]),
        last_bar_low=float(bar["low"]),
    )


def _close_trade(
    *,
    spec: Mapping[str, Any],
    exit_px: float,
    exit_reason: str,
    exit_ts: datetime,
    sleeve: str,
    costs: HarnessCosts,
) -> tuple[dict[str, Any], bool]:
    """Build the trade row and report whether the impact term was unavailable."""
    qty = float(spec["qty"])
    entry_px = float(spec["entry_px"])
    entry_notional = abs(qty) * entry_px
    exit_notional = abs(qty) * exit_px
    impact_missing = False
    leg_bps: list[float] = []
    for notional in (entry_notional, exit_notional):
        impact = _impact_bps(
            notional_usd=notional,
            adv_usd=spec["adv_usd"],
            sigma_daily=spec["sigma_daily"],
            y=costs.impact_y,
        )
        if impact is None:
            impact_missing = True
            impact = 0.0
        leg_bps.append(float(costs.spread_bps) + impact)
    slippage_usd = entry_notional * leg_bps[0] / 10_000.0 + exit_notional * leg_bps[1] / 10_000.0
    fees_usd = (entry_notional + exit_notional) * float(costs.fees_bps) / 10_000.0
    holding_days = max(0.0, (exit_ts - spec["entry_ts"]).total_seconds() / 86_400.0)
    borrow_usd = 0.0
    if spec["side"] == "sell":
        borrow_usd = entry_notional * float(costs.borrow_bps_per_day) / 10_000.0 * holding_days
    gross = (
        qty * (exit_px - entry_px) if spec["side"] == "buy" else qty * (entry_px - exit_px)
    )
    net = gross - slippage_usd - fees_usd - borrow_usd
    risk_usd = qty * float(spec["stop_distance"])
    r_multiple = net / risk_usd if risk_usd > 0 else 0.0
    row = trade_row(
        sleeve=sleeve,
        symbol=spec["symbol"],
        setup=spec["setup"],
        entry_ts=spec["entry_ts"],
        exit_ts=exit_ts,
        qty=qty,
        entry_px=entry_px,
        exit_px=exit_px,
        slippage_bps=(leg_bps[0] + leg_bps[1]) / 2.0,
        fees_usd=fees_usd,
        r_multiple=r_multiple,
        rvol_first5=spec["rvol_first5"],
        adx_14=spec["adx_14"],
        gap_pct=spec["gap_pct"],
        spread_bps=spec["spread_bps"],
        exit_reason=exit_reason,
        gate_verdict=spec["gate_verdict"],
        binding_gate=spec["binding_gate"],
        data_vintage=spec["data_vintage"],
    )
    return row, impact_missing


def replay(
    *,
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    starting_equity: float,
    config: Config,
    gate_factory: Any,
    sizing_caps_factory: Any,
    costs: HarnessCosts,
    sleeve: str = SWING,
    entry_window: Any = None,
    flat_by: Any = None,
) -> HarnessResult:
    """Replay bars in time order through the caller's gate and the sizer.

    ``entry_window`` is an optional ``(start, end)`` time pair; for the intraday
    sleeve it defaults to the configured window and ``flat_by`` to the configured
    flat time. Swing replays pass ``None`` for both.
    """
    by_symbol: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for symbol in sorted(bars_by_symbol):
        bars = tuple(sorted(bars_by_symbol[symbol], key=lambda b: b["ts"]))
        by_symbol[symbol] = bars
    symbols = sorted(by_symbol)

    if entry_window is None and sleeve == INTRADAY:
        entry_window = (config.intraday_entry_after, config.intraday_entry_before)
    if flat_by is None and sleeve == INTRADAY:
        flat_by = config.intraday_flat_by
    window: tuple[time, time] | None = None
    if entry_window is not None:
        start, end = _as_time(entry_window[0]), _as_time(entry_window[1])
        if start is not None and end is not None:
            window = (start, end)
    flat_time = _as_time(flat_by)

    ts_index: dict[str, dict[datetime, int]] = {
        symbol: {bar["ts"]: i for i, bar in enumerate(bars)} for symbol, bars in by_symbol.items()
    }
    timeline = sorted({bar["ts"] for bars in by_symbol.values() for bar in bars})

    skipped: list[dict[str, Any]] = []
    for symbol in symbols:
        bars = by_symbol[symbol]
        if len(bars) < MIN_BARS:
            ts = bars[0]["ts"] if bars else None
            skipped.append(_skip(symbol, "insufficient_bars", ts))

    trades: list[dict[str, Any]] = []
    open_specs: dict[str, dict[str, Any]] = {}
    equity_curve: list[float] = [float(starting_equity)]
    realized = 0.0
    impact_noted = False

    for ts in timeline:
        for symbol in symbols:
            bars = by_symbol[symbol]
            idx = ts_index[symbol].get(ts)
            if idx is None:
                continue
            bar = bars[idx]
            spec = open_specs.get(symbol)
            if spec is not None:
                spec["last"] = float(bar["close"])
                outcome = _exit_for(spec, bar, is_last=(idx == len(bars) - 1), flat_time=flat_time)
                if outcome is not None:
                    exit_px, exit_reason = outcome
                    row, impact_missing = _close_trade(
                        spec=spec,
                        exit_px=exit_px,
                        exit_reason=exit_reason,
                        exit_ts=ts,
                        sleeve=sleeve,
                        costs=costs,
                    )
                    trades.append(row)
                    realized += _row_pnl_usd(row)
                    equity_curve.append(float(starting_equity) + realized)
                    del open_specs[symbol]
                    if impact_missing and not impact_noted:
                        skipped.append(_skip(symbol, "impact_unavailable", ts))
                        impact_noted = True
            if symbol in open_specs or not bar.get("signal"):
                continue
            if idx + 1 >= len(bars):
                skipped.append(_skip(symbol, "no_next_bar", ts))
                continue
            if window is not None and not (window[0] <= ts.time() <= window[1]):
                skipped.append(_skip(symbol, "outside_entry_window", ts))
                continue
            stop = bar.get("stop")
            if stop is None:
                skipped.append(_skip(symbol, "no_stop", ts))
                continue
            entry_px = bars[idx + 1].get("open")
            if entry_px is None:
                skipped.append(_skip(symbol, "no_entry_price", ts))
                continue

            equity = float(starting_equity) + realized
            close = float(bar["close"])
            side = str(bar.get("side", "buy"))
            spread_bps = (
                float(bar["spread_bps"]) if bar.get("spread_bps") is not None
                else float(costs.spread_bps)
            )
            adv_usd = None
            if bar.get("adv_usd") is not None:
                adv_usd = float(bar["adv_usd"])
            elif bar.get("adv_shares") is not None:
                adv_usd = float(bar["adv_shares"]) * close
            request = RiskRequest(
                sleeve=sleeve,
                symbol=symbol,
                setup=str(bar.get("setup", "UNKNOWN")),
                side=side,
                stop_distance=abs(close - float(stop)),
                price=close,
                equity=equity,
                requested_risk_pct=_requested_risk_pct(config, sleeve),
                stop_price=float(stop),
                target_price=float(bar["target"]) if bar.get("target") is not None else None,
                trailing_volume=bar.get("volume"),
            )
            positions = tuple(
                Position(
                    symbol=name,
                    qty=open_specs[name]["qty"],
                    avg_entry=open_specs[name]["entry_px"],
                    sleeve=sleeve,
                    last=open_specs[name]["last"],
                    stop=open_specs[name]["stop"],
                    setup=open_specs[name]["setup"],
                )
                for name in sorted(open_specs)
            )
            deployed = sum(p.notional for p in positions)
            market = _market(symbol, bar, config=config, spread_bps=spread_bps)
            context = HarnessContext(
                symbol=symbol,
                sleeve=sleeve,
                setup=request.setup,
                side=side,
                ts=ts,
                bar=bar,
                bars=bars[: idx + 1],
                market=market,
                request=request,
                book=BookState(
                    equity=equity, cash=max(0.0, equity - deployed), positions=positions
                ),
                config=config,
            )
            decision: GateDecision = gate_factory(context)
            if not decision.allowed:
                skipped.append(
                    _skip(
                        symbol,
                        f"{decision.verdict}:{decision.binding_gate or 'unknown'}",
                        ts,
                        decision.binding_gate,
                    )
                )
                continue
            if decision.adjusted_risk_pct is not None:
                request = replace(request, requested_risk_pct=float(decision.adjusted_risk_pct))
            sized = size(request, caps=sizing_caps_factory(context), vol_scalar=1.0)
            if not sized.ok or sized.qty < 1:
                skipped.append(_skip(symbol, f"size:{sized.reason}", ts))
                continue
            open_specs[symbol] = {
                "symbol": symbol,
                "setup": request.setup,
                "side": side,
                "qty": int(sized.qty),
                "entry_px": float(entry_px),
                "entry_ts": bars[idx + 1]["ts"],
                "stop": float(stop),
                "target": request.target_price,
                "stop_distance": request.stop_distance,
                "last": close,
                "adv_usd": adv_usd,
                "sigma_daily": bar.get("sigma_daily"),
                "rvol_first5": bar.get("rvol_first5"),
                "adx_14": bar.get("adx_14"),
                "gap_pct": bar.get("gap_pct"),
                "spread_bps": spread_bps,
                "gate_verdict": decision.verdict,
                "binding_gate": decision.binding_gate,
                "data_vintage": bar.get("data_vintage"),
            }

    return HarnessResult(
        trades=tuple(trades),
        skipped=tuple(skipped),
        equity_curve=tuple(equity_curve),
        starting_equity=float(starting_equity),
    )


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def summarize(trades: Sequence[Mapping[str, Any]], *, starting_equity: float) -> dict[str, Any]:
    """The plan §10.1 metric table for one sleeve; a missing metric is ``None``.

    Every ``None`` statistic carries a ``<key>_reason`` sibling. USD per-trade PnL
    is reconstructed from the row's prices (long-only, see :func:`_row_pnl_usd`).
    """
    rows = list(trades)
    count = len(rows)
    out: dict[str, Any] = {"trades": count, "starting_equity": float(starting_equity)}
    if count == 0:
        out["ending_equity"] = float(starting_equity)
        for key in _SUMMARY_KEYS:
            out[key] = None
            out[f"{key}_reason"] = "no_trades"
        return out

    pnl = [_row_pnl_usd(r) for r in rows]
    curve = [float(starting_equity)]
    for value in pnl:
        curve.append(curve[-1] + value)
    net_pnl = curve[-1] - float(starting_equity)

    out["net_pnl_usd"] = net_pnl
    out["ending_equity"] = curve[-1]
    out["net_return"] = net_pnl / starting_equity if starting_equity > 0 else None
    if out["net_return"] is None:
        out["net_return_reason"] = "no_starting_equity"

    peak = curve[0]
    max_dd = 0.0
    max_dd_pct = 0.0
    for value in curve:
        peak = max(peak, value)
        dd = peak - value
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak if peak > 0 else 0.0
    out["max_drawdown_usd"] = max_dd
    out["max_drawdown_pct"] = max_dd_pct
    if max_dd_pct > 0:
        out["return_max_dd"] = (out["net_return"] or 0.0) / max_dd_pct
    else:
        out["return_max_dd"] = None
        out["return_max_dd_reason"] = "no_drawdown"

    by_day: dict[Any, float] = {}
    for row, value in zip(rows, pnl, strict=True):
        day = _as_datetime(row["exit_ts"]).date()
        by_day[day] = by_day.get(day, 0.0) + value
    daily = [by_day[day] for day in sorted(by_day)]
    if len(daily) >= 2 and statistics.stdev(daily) > 0:
        daily_returns = [value / starting_equity for value in daily]
        out["sharpe_daily"] = (
            statistics.fmean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(252)
        )
    else:
        out["sharpe_daily"] = None
        out["sharpe_daily_reason"] = "insufficient_daily_observations"

    wins = [value for value in pnl if value > 0]
    losses = [-value for value in pnl if value < 0]
    gross_wins = sum(wins)
    gross_losses = sum(losses)
    out["profit_factor"] = gross_wins / gross_losses if gross_losses > 0 else None
    if out["profit_factor"] is None:
        out["profit_factor_reason"] = "no_losses"
    out["win_rate"] = len(wins) / count
    out["avg_win_usd"] = gross_wins / len(wins) if wins else None
    if out["avg_win_usd"] is None:
        out["avg_win_usd_reason"] = "no_wins"
    out["avg_loss_usd"] = -gross_losses / len(losses) if losses else None
    if out["avg_loss_usd"] is None:
        out["avg_loss_usd_reason"] = "no_losses"
    out["expectancy_per_trade_r"] = statistics.fmean(
        float(r["r_multiple"]) for r in rows
    )
    out["expectancy_per_trade_usd"] = net_pnl / count
    traded = sum(abs(float(r["qty"])) * (float(r["entry_px"]) + float(r["exit_px"])) for r in rows)
    out["turnover"] = traded / starting_equity if starting_equity > 0 else None
    if out["turnover"] is None:
        out["turnover_reason"] = "no_starting_equity"
    out["round_trips"] = count
    out["fees_usd"] = sum(float(r["fees_usd"]) for r in rows)
    out["slippage_bps_mean"] = statistics.fmean(float(r["slippage_bps"]) for r in rows)

    if losses:
        out["es_cvar_975_usd"] = es_historical(losses, 0.975)
    else:
        out["es_cvar_975_usd"] = None
        out["es_cvar_975_usd_reason"] = "no_losses"

    span_start = min(_as_datetime(r["entry_ts"]) for r in rows)
    span_end = max(_as_datetime(r["exit_ts"]) for r in rows)
    span = (span_end - span_start).total_seconds()
    merged = _merged_holding_seconds(rows)
    if span > 0:
        out["exposure_pct"] = min(1.0, merged / span)
    else:
        out["exposure_pct"] = None
        out["exposure_pct_reason"] = "no_span"
    return out


def _merged_holding_seconds(rows: Sequence[Mapping[str, Any]]) -> float:
    """Union of the trades' holding intervals, in seconds (exposure numerator)."""
    intervals = sorted(
        (_as_datetime(r["entry_ts"]), _as_datetime(r["exit_ts"]))
        for r in rows
        if _as_datetime(r["exit_ts"]) > _as_datetime(r["entry_ts"])
    )
    total = 0.0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for start, end in intervals:
        if current_start is None:
            current_start, current_end = start, end
        elif start <= current_end:  # type: ignore[operator]
            current_end = max(current_end, end)  # type: ignore[type-var]
        else:
            total += (current_end - current_start).total_seconds()  # type: ignore[operator]
            current_start, current_end = start, end
    if current_start is not None and current_end is not None:
        total += (current_end - current_start).total_seconds()
    return total
