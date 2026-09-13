"""Synthetic protective stops: stop-limit placement, MAE/ATR distance, cluster avoidance.

Invariants enforced (plan §5.2, design §10 "Stop distance" / "Stop placement"):

* A protective exit is **always a stop-LIMIT** whose limit sits on the safe
  side of its trigger. A naked stop-market is unrepresentable here.
* ``stop_distance`` is one formula - ``max(atr_mult * ATR, MAE_pctl)`` floored
  at ``1.0 * ATR`` - and the only place it is computed.
* A stop level is never parked at a round number, the prior day's high/low, or
  a venue band edge: ``cluster_clearance`` nudges it one tick away and flags it.
* Nothing is invented: a position without a reference price or a trigger level
  yields no leg.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..risk.state import MarketState, Position

if TYPE_CHECKING:  # the guard is a sibling; never imported at module load
    from .guard import OrderIntent

#: One venue tick, and the nudge distance used to step off a stop cluster.
TICK = 0.01

#: The only stop kind this module emits (design §10).
STOP_KIND = "synthetic_stop_limit"

#: Round-number grid that attracts stop clusters: whole and half dollars.
ROUND_STEP = 0.50

#: LULD band proximity (design §10: "never within 5 bps of a band edge").
LULD_PROXIMITY_BPS = 5.0


def _round_price(price: float) -> float:
    """Quantize to the venue grid via the price module (one rounding rule)."""
    from .prices import round_price

    return round_price(price)


def _deviation_bps(config) -> float:
    """The marketable deviation cap with no spread context (policy-owned)."""
    from .policy import deviation_bps

    return deviation_bps(None, config=config)


def stop_distance(*, atr: float, mae_pctl: float, config) -> float:
    """Stop distance in price units: ``max(stop_atr_mult*ATR, MAE_pctl)``,
    floored at ``1.0 * ATR`` (design §10).

    A non-positive or non-finite ATR is a broken input, never a distance:
    raise rather than emit a level.
    """
    if not math.isfinite(atr) or atr <= 0.0:
        raise ValueError(f"ATR must be finite and positive: {atr!r}")
    if not math.isfinite(mae_pctl) or mae_pctl < 0.0:
        raise ValueError(f"MAE percentile must be finite and >= 0: {mae_pctl!r}")
    return max(config.stop_atr_mult * atr, mae_pctl, atr)


def _cluster_levels(price: float, market: MarketState) -> list[float]:
    """Cluster levels within one tick of ``price`` that a stop must avoid."""
    levels: list[float] = []
    nearest = round(price / ROUND_STEP) * ROUND_STEP
    if abs(price - nearest) <= TICK:
        levels.append(nearest)
    for prior in (market.last_bar_high, market.last_bar_low):
        if prior is not None and math.isfinite(prior) and abs(price - prior) <= TICK:
            levels.append(float(prior))
    # Venue band edges: the frozen MarketState carries no band fields today, so
    # this activates only when a richer market adapter supplies them. Absent
    # bands are not invented - the check simply contributes no level.
    for attr in ("luld_upper", "luld_lower"):
        edge = getattr(market, attr, None)
        if edge is None or not math.isfinite(edge) or edge <= 0.0:
            continue
        if abs(price - edge) <= edge * LULD_PROXIMITY_BPS / 10_000.0:
            levels.append(float(edge))
    return levels


def cluster_clearance(price: float, *, market: MarketState, config) -> float:
    """Move a stop one tick off a cluster (round number / prior extreme / band edge).

    Returns ``price`` unchanged when it is clear of every cluster. When it is
    at or below the nearest cluster the level steps **down**, otherwise up -
    always one tick away from the flow, never onto it.
    """
    if not math.isfinite(price) or price <= 0.0:
        raise ValueError(f"stop level must be finite and positive: {price!r}")
    levels = _cluster_levels(price, market)
    if not levels:
        return price
    nearest = min(levels, key=lambda lvl: abs(price - lvl))
    moved = price - TICK if price <= nearest else price + TICK
    return _round_price(moved)


def protective_limit(
    trigger: float, side: str, deviation_bps: float, *, reference: float
) -> float:
    """The stop-limit price: one deviation cap on the safe side of ``trigger``.

    For a ``sell`` (long protection) the limit sits *below* the trigger; for a
    ``buy`` (short protection) *above*. The cap is ``reference * bps``; if
    rounding would collapse the limit onto the trigger it is stepped one tick
    further so the ordering is never lost.
    """
    side = (side or "").strip().lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"unknown protective side: {side!r}")
    if not math.isfinite(reference) or reference <= 0.0:
        raise ValueError(f"reference price must be finite and positive: {reference!r}")
    offset = abs(reference) * abs(deviation_bps) / 10_000.0
    trigger = _round_price(trigger)
    if side == "sell":
        limit = _round_price(trigger - offset)
        if limit >= trigger:
            limit = _round_price(trigger - TICK)
    else:
        limit = _round_price(trigger + offset)
        if limit <= trigger:
            limit = _round_price(trigger + TICK)
    return limit


@dataclass(frozen=True)
class StopPlan:
    """A synthetic protective stop-limit for one open position (design §10)."""

    trigger_price: float
    limit_price: float
    qty: int
    stop_kind: str
    adjusted_for_cluster: bool
    reason: str


def plan_stop(
    position: Position,
    *,
    atr: float,
    side: str,
    deviation_bps: float,
    config,
    market: MarketState,
) -> StopPlan:
    """Plan the synthetic stop-limit for ``position`` (design §10).

    The entry mark is the broker's ``last`` when present, else ``avg_entry``.
    The MAE floor is the configured percentile applied to ATR when the setup's
    measured MAE is not supplied to this function; ATR therefore always sets a
    floor of one ATR and normally the binding ``stop_atr_mult`` multiple.
    """
    side = (side or "").strip().lower()
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short': {side!r}")
    reference = position.last if position.last is not None else position.avg_entry
    if reference is None or not math.isfinite(reference) or reference <= 0.0:
        raise ValueError(f"no usable reference price for {position.symbol!r}")
    distance = stop_distance(
        atr=atr, mae_pctl=config.stop_mae_pctl * atr, config=config
    )
    raw = reference - distance if side == "long" else reference + distance
    cleared = cluster_clearance(raw, market=market, config=config)
    adjusted = cleared != raw
    trigger = _round_price(cleared)
    limit = protective_limit(
        trigger, "sell" if side == "long" else "buy", deviation_bps, reference=reference
    )
    qty = int(abs(position.qty))
    reason = (
        f"{side} stop distance {distance:.4f} from {reference:.4f} "
        f"(atr={atr:.4f}, deviation={abs(deviation_bps):.1f}bps"
        + (", moved off cluster)" if adjusted else ")")
    )
    return StopPlan(
        trigger_price=trigger,
        limit_price=limit,
        qty=qty,
        stop_kind=STOP_KIND,
        adjusted_for_cluster=adjusted,
        reason=reason,
    )


def protective_orders(
    positions: Iterable[Position],
    *,
    stops: Mapping[str, float],
    config,
) -> tuple[OrderIntent, ...]:
    """One synthetic stop-limit leg per unprotected open position (plan §4.4 C18).

    ``stops`` is the registry of positions that already hold a working
    protective order: its keys are covered symbols and its values their live
    stop price. A position is skipped when covered, when it is flat, or when
    it carries no trigger level (``Position.stop``) - a level is never invented.
    """
    from .guard import OrderIntent, client_order_id

    covered = {str(sym) for sym in stops}
    deviation = _deviation_bps(config)
    created_at = config.now().isoformat()
    out: list[OrderIntent] = []
    for position in sorted(positions, key=lambda p: str(p.symbol)):
        if not position.qty:
            continue
        if str(position.symbol) in covered:
            continue
        level = position.stop
        if level is None or not math.isfinite(level) or level <= 0.0:
            continue
        side = "sell" if position.qty > 0 else "buy"
        reference = position.last if position.last is not None else position.avg_entry
        if reference is None or not math.isfinite(reference) or reference <= 0.0:
            reference = level
        limit = protective_limit(level, side, deviation, reference=reference)
        cid = client_order_id(position.sleeve, position.symbol, "stop")
        out.append(
            OrderIntent(
                intent_id=f"stop-{cid}",
                sleeve=position.sleeve,
                symbol=position.symbol,
                side=side,
                qty=int(abs(position.qty)),
                order_type="stop_limit",
                limit_price=limit,
                stop_price=_round_price(level),
                tif="day",
                client_order_id=cid,
                created_at=created_at,
            )
        )
    return tuple(out)
