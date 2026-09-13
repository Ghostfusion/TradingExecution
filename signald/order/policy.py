"""Child-order policy: how one sized intent becomes broker child orders.

Invariant (plan §4.4, design §8.1–§8.2): an entry is *never* a market order.
Breakout/continuation setups cross with a **marketable limit** whose price is
capped at a spread-derived deviation; mean-reversion setups rest passively and
never cross the touch. Every price is quantized to the venue's legal grid
(≥$1 → 2dp, <$1 → 4dp) before it can reach the broker, and the child orders
always sum exactly to the requested quantity — a shortfall or an unsliceable
size raises rather than rounding in the wrong direction.

This module owns exactly two computations: the child-order slice count/sizes
(§4.4) and the marketable deviation cap (design §8.1). The price tick rule is
owned by :mod:`signald.order.prices` and imported here. It reads market state
and the risk request but performs no I/O, and it can only ever emit ``limit``
or ``marketable_limit`` children.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..risk.state import MarketState, RiskRequest
from .prices import round_price, tick_for

#: Every execution style this policy understands (closed vocabulary).
EXECUTION_STYLES = ("marketable_limit", "resting_limit")

#: Deviation floor (bps): the spread is the thing the order must pay, so the
#: cap is never below this even on a quiet, narrow book.
_MIN_DEVIATION_BPS = 5.0

#: One child never exceeds this fraction of a known 20-day ADV (plan §4.4).
PARTICIPATION_CAP = 0.05
#: Children per parent, and the hard cap on slicing.
MAX_CHILDREN = 5

#: Setup → style. Breakout/continuation alpha decays fast, so it crosses;
#: mean reversion *is* the spread, so it rests. Anything unrecognised takes the
#: conservative marketable path rather than resting an unvetted thesis.
_RESTING_SETUPS = frozenset({"VWAP_REVERT", "GAP_FADE"})


@dataclass(frozen=True)
class ChildOrder:
    """One broker order leg under a :class:`OrderPlan`."""

    seq: int
    qty: int
    order_type: str  # "limit" | "marketable_limit"
    limit_price: float
    notional_usd: float
    style: str


@dataclass(frozen=True)
class OrderPlan:
    """The child orders for one sized intent, plus why the price was chosen."""

    orders: tuple[ChildOrder, ...]
    style: str
    rationale: str
    deviation_bps: float  # the cap the limit was placed at


def style_for(setup: str) -> str:
    """Execution style for a setup name; unknown setups cross (conservative)."""
    name = (setup or "").strip().upper()
    if name in _RESTING_SETUPS:
        return "resting_limit"
    return "marketable_limit"


def deviation_bps(spread_bps: float | None, *, config) -> float:
    """The deviation cap a marketable limit may accept, in bps (design §8.1).

    ``max(2 * spread, cost_liquid_bps / 2)`` floored at 5 bps: crossing pays
    the spread, and the cap is twice it so a blip does not strand the fill.
    A missing spread never *widens* the cap (no invented number) — it leaves
    the cost-band floor in force.
    """
    cap = max(_MIN_DEVIATION_BPS, config.cost_liquid_bps / 2.0)
    if spread_bps is not None and math.isfinite(spread_bps):
        cap = max(cap, 2.0 * spread_bps)
    return cap


def _slice_count(qty: int, adv_shares: float | None) -> int:
    """Number of children: cap participation at 5% ADV, at most 5, never > qty.

    The gate refuses an order without ADV, so an unknown ADV here means the
    caller bypassed the gate: fail safe by not slicing (one child).
    """
    if adv_shares is None or not math.isfinite(adv_shares) or adv_shares <= 0.0:
        return 1
    per_child = PARTICIPATION_CAP * adv_shares
    count = math.ceil(qty / per_child)
    count = min(MAX_CHILDREN, max(1, count))
    return min(count, qty)


def _child_sizes(qty: int, count: int) -> tuple[int, ...]:
    """Equal children, last one absorbing the remainder (sum == ``qty``)."""
    base, remainder = divmod(qty, count)
    return tuple(base for _ in range(count - 1)) + (base + remainder,)


def _limit_price(price: float, side: str, deviation: float, style: str) -> float:
    """Place the limit: cross with a capped deviation, or rest on the touch.

    ``round_price`` snaps to the venue grid. A marketable order must still
    cross the touch, so one tick is added only when plain rounding would leave
    it on the wrong side (a rounded price is otherwise kept as the formula
    computed it, never widened past the cap).
    """
    if style == "marketable_limit":
        raw = price * (1.0 + deviation / 1e4) if side == "buy" else price * (1.0 - deviation / 1e4)
        limit = round_price(raw)
        if side == "buy" and limit <= price:
            return round_price(limit + tick_for(limit))
        if side == "sell" and limit >= price:
            return round_price(limit - tick_for(limit))
        return limit
    limit = round_price(price)
    if side == "buy" and limit > price:
        return round_price(limit - tick_for(limit))
    if side == "sell" and limit < price:
        return round_price(limit + tick_for(limit))
    return limit


def _build(
    request: RiskRequest,
    qty: int,
    *,
    market: MarketState,
    config,
    style: str,
    urgent: bool,
) -> OrderPlan:
    if qty < 1:
        raise ValueError(f"order quantity must be >= 1 share: {qty!r}")
    price = market.last
    if price is None or not math.isfinite(price) or price <= 0.0:
        raise ValueError("market.last is required to price child orders")
    side = (request.side or "").strip().lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"unknown order side: {request.side!r}")
    if style not in EXECUTION_STYLES:
        raise ValueError(f"unknown execution style: {style!r}")

    deviation = deviation_bps(market.spread_bps, config=config)
    if urgent:
        # A forced exit must clear the spread even in a thin book: floor the
        # cap at the wide (low-float) round-trip band instead of the liquid one.
        deviation = max(deviation, config.cost_lowfloat_bps)

    count = _slice_count(qty, market.adv_shares)
    sizes = _child_sizes(qty, count)
    order_type = "limit" if style == "resting_limit" else "marketable_limit"
    limit = _limit_price(price, side, deviation, style)
    orders = tuple(
        ChildOrder(
            seq=i,
            qty=size,
            order_type=order_type,
            limit_price=limit,
            notional_usd=round(size * limit, 2),
            style=style,
        )
        for i, size in enumerate(sizes)
    )
    setup = (request.setup or "unknown").strip() or "unknown"
    rationale = (
        f"{style} {side} {qty} {request.symbol} as {count} child order(s) "
        f"at <= {deviation:g} bps deviation ({setup})"
    )
    return OrderPlan(orders=orders, style=style, rationale=rationale, deviation_bps=deviation)


def plan_entry(
    request: RiskRequest,
    qty: int,
    *,
    market: MarketState,
    config,
    style: str | None = None,
) -> OrderPlan:
    """Child orders for entering a position; ``style`` overrides the setup map."""
    chosen = style if style is not None else style_for(request.setup)
    return _build(request, qty, market=market, config=config, style=chosen, urgent=False)


def plan_exit(
    request: RiskRequest,
    qty: int,
    *,
    market: MarketState,
    config,
    urgent: bool = False,
) -> OrderPlan:
    """Child orders for leaving a position: a marketable limit (design §8.1).

    ``urgent`` widens the deviation cap to the low-float cost band so a forced
    exit (stop, flatten, time) cannot be left unfilled by a routine spread.
    """
    return _build(
        request,
        qty,
        market=market,
        config=config,
        style="marketable_limit",
        urgent=urgent,
    )
