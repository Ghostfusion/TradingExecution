"""EOD flatten and flat verification (plan §4.4 C20, design §13).

Invariants enforced:

* **Nothing here declares the book flat except** :func:`verify_flat`, which
  re-reads the broker positions it is handed and never assumes.
* A flatten order is never a naked market order: it is a marketable limit on
  the safe side of the last price, or a closing-auction ``cls`` limit when the
  venue cutoff has not passed.
* A position with no usable price is **not** dropped and is never given an
  invented level: it stays in the plan flagged ``needs_manual`` and the plan
  reason names it, so the caller cannot report flat.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..config import parse_hhmm
from ..risk.state import Position
from .stops import protective_limit

if TYPE_CHECKING:  # the guard is a sibling; never imported at module load
    from .guard import OrderIntent

#: A residual at or below this many shares is dust, not a position.
DUST_SHARES = 1.0

#: Order-type/TIF markers.
MARKETABLE_LIMIT = "marketable_limit"
NEEDS_MANUAL = "needs_manual"
DAY_TIF = "day"
CLOSING_AUCTION_TIF = "cls"


@dataclass(frozen=True)
class FlattenPlan:
    """The legs that take the intraday book flat, and how they are priced."""

    orders: tuple[OrderIntent, ...]
    use_closing_auction: bool
    reason: str


def _to_et(now: datetime, calendar: Any) -> datetime:
    """Convert ``now`` to ET, or document that it is already ET.

    Uses an injected calendar's ``to_et`` when it exposes one (including the
    ``signald.marketdata.calendar`` module); otherwise ``now`` is treated as
    the ET clock itself - the injected-clock convention used across the suite.
    """
    if calendar is not None:
        to_et = getattr(calendar, "to_et", None)
        if callable(to_et):
            return to_et(now)
        # An explicitly supplied calendar with no conversion hook means the
        # injected stamp already is the ET clock.
        return now
    try:
        from ..marketdata import calendar as market_calendar
    except ImportError:
        return now
    to_et = getattr(market_calendar, "to_et", None)
    return to_et(now) if callable(to_et) else now


def seconds_to_flat(now: datetime, *, config, calendar: Any = None) -> float:
    """Seconds until ``config.intraday_flat_by`` (ET); negative once past it."""
    et = _to_et(now, calendar)
    flat_at = parse_hhmm(config.intraday_flat_by)
    cutoff = et.replace(
        hour=flat_at.hour, minute=flat_at.minute, second=0, microsecond=0
    )
    return (cutoff - et).total_seconds()


def is_flat(
    positions: Iterable[Position], *, dust_shares: float = DUST_SHARES
) -> tuple[bool, tuple[str, ...]]:
    """True only when every position is within ``dust_shares`` of zero.

    Returns the offending symbols (sorted) alongside the verdict.
    """
    residual = sorted(
        {str(p.symbol) for p in positions if abs(float(p.qty)) > dust_shares}
    )
    return (not residual, tuple(residual))


def _leg(
    position: Position,
    order_type: str,
    *,
    qty: int,
    limit_price: float | None,
    stop_price: float | None,
    tif: str,
    now: datetime,
) -> OrderIntent:
    from .guard import OrderIntent, client_order_id

    cid = client_order_id(position.sleeve, position.symbol, "flat")
    return OrderIntent(
        intent_id=f"flat-{cid}",
        sleeve=position.sleeve,
        symbol=position.symbol,
        side="sell" if position.qty > 0 else "buy",
        qty=qty,
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        tif=tif,
        client_order_id=cid,
        created_at=now.isoformat(),
    )


def flatten_plan(
    positions: Iterable[Position],
    *,
    now: datetime,
    config,
    calendar: Any = None,
    closing_auction_eligible: bool = True,
) -> FlattenPlan:
    """Plan the EOD flatten: closing auction when eligible and open, else
    marketable limits at the last price with the deviation cap (plan §4.4 C20).

    A position with no usable price yields a ``needs_manual`` leg with no
    price, and its symbol is named in the reason.
    """
    from .policy import deviation_bps

    positions = tuple(positions)
    flat, _ = is_flat(positions)
    if flat:
        return FlattenPlan(orders=(), use_closing_auction=False, reason="already flat")

    # The venue's closing-auction cutoff is the flat-by moment: once we are
    # past it the remaining risk is marketable limits, not an auction order.
    use_auction = bool(closing_auction_eligible) and seconds_to_flat(
        now, config=config, calendar=calendar
    ) > 0.0
    tif = CLOSING_AUCTION_TIF if use_auction else DAY_TIF
    deviation = deviation_bps(None, config=config)

    orders: list[OrderIntent] = []
    manual: list[str] = []
    for position in sorted(positions, key=lambda p: str(p.symbol)):
        if abs(float(position.qty)) <= DUST_SHARES:
            continue
        qty = int(abs(position.qty))
        price = position.last
        if qty < 1 or price is None or not math.isfinite(price) or price <= 0.0:
            manual.append(str(position.symbol))
            orders.append(
                _leg(
                    position,
                    NEEDS_MANUAL,
                    qty=max(qty, 1),
                    limit_price=None,
                    stop_price=None,
                    tif=tif,
                    now=now,
                )
            )
            continue
        limit = protective_limit(
            price, "sell" if position.qty > 0 else "buy", deviation, reference=price
        )
        orders.append(
            _leg(
                position,
                MARKETABLE_LIMIT,
                qty=qty,
                limit_price=limit,
                stop_price=None,
                tif=tif,
                now=now,
            )
        )

    route = "closing auction" if use_auction else "marketable limits"
    reason = f"{len(orders)} flatten leg(s) via {route}"
    if manual:
        reason += f"; needs_manual: {','.join(sorted(manual))}"
    return FlattenPlan(
        orders=tuple(orders), use_closing_auction=use_auction, reason=reason
    )


def verify_flat(
    broker_positions: Iterable[Position] | Callable[[], Iterable[Position] | None],
    *,
    now: datetime,
    config,
    attempts: int = 1,
) -> tuple[bool, str]:
    """The only authority that may declare the book flat (plan §4.4 C20).

    Re-reads the broker positions it is handed on every attempt (pass a
    zero-arg callable for a true re-read). A missing read is ``unavailable``
    and is *not* flat; a residual is never rounded away.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1: {attempts!r}")

    if callable(broker_positions):
        source = broker_positions
    else:
        snapshot = tuple(broker_positions)

        def source() -> tuple[Position, ...]:  # type: ignore[misc]
            return snapshot

    def reader() -> tuple[Position, ...] | None:
        return _materialise(source())

    detail = "flat"
    for _ in range(attempts):
        read = reader()
        if read is None:
            return False, "broker positions unavailable"
        flat, residual = is_flat(read)
        if flat:
            return True, "flat"
        detail = f"residual: {','.join(residual)}"
    return False, detail


def _materialise(source: Iterable[Position] | None) -> tuple[Position, ...] | None:
    return None if source is None else tuple(source)
