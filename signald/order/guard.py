"""The pre-trade OrderGuard (plan §4.4, design §14 "SEC 15c3-5 in miniature"").

The guard is the last thing between a sized ``OrderIntent`` and the broker. It
evaluates a fixed, ordered table of checks and returns a :class:`GuardResult`
whose ``binding`` is the **first** failing check - so a denial is never vague.

Invariants:

* **Fail closed.** A check that cannot evaluate (no gate verdict, no mandate,
  no approval when one is required) refuses. An exception raised inside a
  check denies with ``check_error`` - never a pass, never a crash for the
  caller.
* **No market orders by construction.** ``market`` is not in the allowed
  order-type set, so an intent that could not carry a limit cannot be sent.
* **Every failure is reported.** ``reasons`` carries one line per failing
  check, in table order, even though only the first becomes ``binding``.
* **One computation.** The price tick rule is owned by ``signald.order.prices``
  (:func:`price_is_on_grid`); the guard imports it rather than re-deriving the
  grid. The gate is not re-implemented here - the caller passes its verdict
  and a ``BLOCK``/missing verdict denies.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import Config
from ..mandate import Mandate
from ..risk.state import BookState, MarketState

#: Order types the order path may emit (plan §4.4: limit/marketable-limit).
#: ``market`` is deliberately absent - a market order is denied by construction.
ALLOWED_ORDER_TYPES = ("limit", "marketable_limit", "stop", "stop_limit")

#: TIFs legal for a two-sleeve day-trading book (plan §4.4 bracket legality).
#: ``cls`` is the closing-auction TIF the flatten planner chooses when it routes
#: a leg through the close (``order/flatten.py:CLOSING_AUCTION_TIF``, plan
#: §4.6) - it was missing here, so the plan's own auction preference could not
#: have passed this check. ``opg`` and the IOC/FOK family stay out: nothing in
#: this book emits them, and adding a TIF is a safety-surface decision each time
#: (owner decision 2026-09-25: day, gtc, cls).
ALLOWED_TIFS = ("day", "gtc", "cls")

#: Order types legal in extended hours.
EXTENDED_HOURS_ORDER_TYPES = ("limit", "marketable_limit")

#: Verdicts that permit submission (the gate's ``allowed`` set).
PERMITTING_VERDICTS = ("ALLOW", "REDUCE")

#: The protective order type whose own trigger *is* the stop, so a separate
#: ``stop_price`` is not required on the entry.
PROTECTIVE_ORDER_TYPE = "stop_limit"

#: Charset and length bound for a broker client order id.
_ID_MAX = 48
_ID_OK = re.compile(r"[A-Za-z0-9-]+\Z")
_ID_STRIP = re.compile(r"[^A-Za-z0-9-]")


def client_order_id(sleeve: str, key: str, leg: str) -> str:
    """Deterministic, broker-safe id: ``f"{sleeve[:3]}-{key[:32]}-{leg}"``.

    Sanitised to ``[A-Za-z0-9-]`` and truncated to 48 chars, so identical
    inputs always yield an identical id and the id is always within the
    broker's limit.
    """
    head = _ID_STRIP.sub("", str(sleeve))[:3]
    body = _ID_STRIP.sub("", str(key))[:32]
    tail = _ID_STRIP.sub("", str(leg))
    return f"{head}-{body}-{tail}"[:_ID_MAX]


@dataclass(frozen=True)
class OrderIntent:
    """A sized, structured order the policy selector produced (plan §2.4, §4.4)."""

    intent_id: str
    sleeve: str
    symbol: str
    side: str  # "buy" | "sell"
    qty: int
    order_type: str  # "limit" | "marketable_limit" | "stop" | "stop_limit"
    limit_price: float | None
    stop_price: float | None
    tif: str  # "day" | "gtc" | "ioc" | "fok" | "opg" | "cls"
    client_order_id: str
    created_at: str
    parent_proposal_id: str | None = None
    extended_hours: bool = False

    @property
    def notional_usd(self) -> float:
        """Order notional using the binding price (limit first, else stop).

        Zero when the intent carries no price at all; the guard then falls
        back to the reference mark for buying-power purposes.
        """
        price = self.limit_price if self.limit_price is not None else self.stop_price
        if price is None:
            return 0.0
        return abs(float(price)) * abs(self.qty)


@dataclass(frozen=True)
class GuardResult:
    """The guard's verdict: ``ok``, every failure, and the first one."""

    ok: bool
    reasons: tuple[str, ...]
    binding: str | None


@dataclass(frozen=True)
class _Context:
    """Everything a check may read. Pure data - the guard performs no I/O."""

    book: BookState
    market: MarketState
    mandate: Mandate
    config: Config
    gate_verdict: str | None
    approval_id: str | None
    requires_approval: bool
    open_orders: tuple[dict[str, Any], ...]
    other_sleeve_side: str | None
    price_of: float | None


# --------------------------------------------------------------------------
# individual checks: return a failure reason or None
# --------------------------------------------------------------------------
def _client_order_id(intent: OrderIntent, ctx: _Context) -> str | None:
    cid = intent.client_order_id
    if not cid:
        return "client_order_id is empty"
    if len(cid) > _ID_MAX:
        return f"client_order_id is {len(cid)} chars (> {_ID_MAX})"
    if not _ID_OK.match(cid):
        return f"client_order_id has illegal characters: {cid!r}"
    return None


def _qty(intent: OrderIntent, ctx: _Context) -> str | None:
    qty = intent.qty
    if isinstance(qty, bool) or not isinstance(qty, int):
        return f"qty must be an int, got {type(qty).__name__}"
    if qty < 1:
        return f"qty must be >= 1, got {qty}"
    return None


def _order_type(intent: OrderIntent, ctx: _Context) -> str | None:
    if intent.order_type not in ALLOWED_ORDER_TYPES:
        return f"order_type {intent.order_type!r} is not one of {list(ALLOWED_ORDER_TYPES)}"
    return None


def _precision(intent: OrderIntent, ctx: _Context) -> str | None:
    # Lazily imported: prices.py owns the tick rule, the guard only checks it.
    from .prices import price_is_on_grid

    for label, price in (("limit_price", intent.limit_price), ("stop_price", intent.stop_price)):
        if price is None:
            continue
        if not price_is_on_grid(float(price)):
            return f"{label}={price} is not on the price tick grid"
    return None


def _parse_ts(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _mandate(intent: OrderIntent, ctx: _Context) -> str | None:
    mandate = ctx.mandate
    if mandate is None:
        return "no mandate"
    if intent.side not in ("buy", "sell"):
        return f"side {intent.side!r} is not buy/sell"
    allowed = {str(s).upper() for s in mandate.allowed}
    if str(intent.symbol).upper() not in allowed:
        return f"{intent.symbol} is not in the mandate"
    created = _parse_ts(intent.created_at)
    if created is None:
        return f"created_at {intent.created_at!r} is not a timestamp"
    if mandate.is_expired(created):
        return f"mandate {mandate.id} expired {mandate.expires.isoformat()}"
    if intent.side == "sell" and ctx.book.position_for(intent.symbol) <= 0 and not mandate.shorts:
        return f"sell would open a short in {intent.symbol}; mandate forbids shorts"
    return None


def _gate_verdict(intent: OrderIntent, ctx: _Context) -> str | None:
    verdict = ctx.gate_verdict
    if verdict is None:
        return "no gate verdict (fail closed)"
    if verdict not in PERMITTING_VERDICTS:
        return f"gate verdict {verdict!r} is not one of {list(PERMITTING_VERDICTS)}"
    return None


def _approval(intent: OrderIntent, ctx: _Context) -> str | None:
    if ctx.requires_approval and not ctx.approval_id:
        return "approval required but no approval_id"
    return None


def _effective_notional(intent: OrderIntent, ctx: _Context) -> float:
    """Order notional, falling back to the reference mark when it has no price."""
    notional = intent.notional_usd
    if notional > 0:
        return notional
    mark = ctx.price_of if ctx.price_of is not None else ctx.market.last
    if mark is None:
        return 0.0
    return abs(float(mark)) * abs(intent.qty)


def _buying_power(intent: OrderIntent, ctx: _Context) -> str | None:
    notional = _effective_notional(intent, ctx)
    buying_power = ctx.book.buying_power
    if buying_power is not None and notional > float(buying_power):
        return f"notional {notional:.2f} exceeds buying power {buying_power}"
    if intent.side == "buy":
        projected = ctx.book.notional() + notional
        cap = ctx.config.leverage_cap * ctx.book.equity
        if projected > cap:
            return (
                f"projected exposure {projected:.2f} exceeds "
                f"{ctx.config.leverage_cap}x equity ({cap:.2f})"
            )
    return None


def _bracket_legality(intent: OrderIntent, ctx: _Context) -> str | None:
    if intent.tif not in ALLOWED_TIFS:
        return f"tif {intent.tif!r} is not one of {list(ALLOWED_TIFS)}"
    if intent.extended_hours:
        if intent.tif != "day":
            return "extended-hours orders must be day tif"
        # A stop leg is denied here too: its type is not in the allowed set.
        if intent.order_type not in EXTENDED_HOURS_ORDER_TYPES:
            return f"extended-hours orders cannot be {intent.order_type!r}"
    return None


def _stop_present(intent: OrderIntent, ctx: _Context) -> str | None:
    if intent.order_type != PROTECTIVE_ORDER_TYPE and intent.stop_price is None:
        return "intent carries no protective stop_price"
    return None


def _duplicate(intent: OrderIntent, ctx: _Context) -> str | None:
    open_ids = {o.get("client_order_id") for o in ctx.open_orders}
    if intent.client_order_id in open_ids:
        return f"client_order_id {intent.client_order_id!r} is already open"
    return None


def _wash(intent: OrderIntent, ctx: _Context) -> str | None:
    other = ctx.other_sleeve_side
    if other is not None and other != intent.side:
        return f"opposite sleeve side {other!r} would self-cross {intent.side!r}"
    return None


#: Ordered check table; the first failure becomes ``binding``.
CHECKS: dict[str, Callable[[OrderIntent, _Context], str | None]] = {
    "client_order_id": _client_order_id,
    "qty": _qty,
    "order_type": _order_type,
    "precision": _precision,
    "mandate": _mandate,
    "gate_verdict": _gate_verdict,
    "approval": _approval,
    "buying_power": _buying_power,
    "bracket_legality": _bracket_legality,
    "stop_present": _stop_present,
    "duplicate": _duplicate,
    "wash": _wash,
}


def guard(
    intent: OrderIntent,
    *,
    book: BookState,
    market: MarketState,
    mandate: Mandate,
    config: Config,
    gate_verdict: str | None = None,
    approval_id: str | None = None,
    requires_approval: bool = False,
    open_orders: tuple[dict[str, Any], ...] = (),
    other_sleeve_side: str | None = None,
    price_of: float | None = None,
) -> GuardResult:
    """Evaluate every check; collect all reasons and name the first failure.

    ``gate_verdict`` is the house gate's verdict for this intent: ``None``
    means the caller did not run the gate and denies (fail closed).
    """
    ctx = _Context(
        book=book,
        market=market,
        mandate=mandate,
        config=config,
        gate_verdict=gate_verdict,
        approval_id=approval_id,
        requires_approval=requires_approval,
        open_orders=open_orders,
        other_sleeve_side=other_sleeve_side,
        price_of=price_of,
    )
    reasons: list[str] = []
    binding: str | None = None
    for name, check in CHECKS.items():
        try:
            failure = check(intent, ctx)
        except Exception as exc:  # noqa: BLE001 - a check error must deny, never pass
            failure = f"check_error: {exc}"
        if failure:
            reasons.append(f"{name}: {failure}")
            if binding is None:
                binding = name
    return GuardResult(ok=not reasons, reasons=tuple(reasons), binding=binding)
