"""Order state machine over broker ``trade_updates`` (plan §4.4, §9.2 golden lifecycle).

Invariant: local order state is a monotone, broker-derived function of the
``trade_updates`` stream. It never regresses, it is never guessed, and every
event that cannot be applied faithfully is recorded as an anomaly *with the raw
broker event* while the state is left exactly where it was (fail closed, design
§1.2). This is the only module that interprets a ``trade_updates`` event: the
order manager, the reconciler and the stop manager all read the state it
produces, so a fill is counted here once and nowhere else.

The broker body is authoritative. ``order.status`` is the closed set
:data:`STATES`; ``order.filled_qty`` is cumulative and must agree with the sum
of the individual fill events; ``order.filled_avg_price`` is the broker's
weighted average and is used when present, otherwise the weighted average is
computed from the individual fills. A fill event for a ``client_order_id`` we
have not submitted yet still creates state - the trade update can beat our own
submit row back and must never be thrown away.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..stores import AuditChain

#: The closed set of order statuses (plan §4.4). An unknown status is an anomaly.
STATES: tuple[str, ...] = (
    "new",
    "accepted",
    "partially_filled",
    "filled",
    "canceled",
    "rejected",
    "expired",
    "replaced",
    "done_for_day",
)

#: A status with no successor: the order is finished.
TERMINAL_STATES: frozenset[str] = frozenset(
    {"filled", "canceled", "rejected", "expired", "replaced", "done_for_day"}
)

#: Legal ``from -> to`` transitions. ``s -> s`` is a tolerated idempotent
#: redelivery; any other pair not listed here is an anomaly and does not move
#: the state. ``filled``/``canceled``/``rejected``/``expired``/``replaced``/
#: ``done_for_day`` accept nothing but themselves.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset(
        {
            "new", "accepted", "partially_filled", "filled", "canceled",
            "rejected", "expired", "replaced", "done_for_day",
        }
    ),
    "accepted": frozenset(
        {
            "accepted", "partially_filled", "filled", "canceled",
            "rejected", "expired", "replaced", "done_for_day",
        }
    ),
    "partially_filled": frozenset(
        {"partially_filled", "filled", "canceled", "expired", "replaced", "done_for_day"}
    ),
    "filled": frozenset({"filled"}),
    "canceled": frozenset({"canceled"}),
    "rejected": frozenset({"rejected"}),
    "expired": frozenset({"expired"}),
    "replaced": frozenset({"replaced"}),
    "done_for_day": frozenset({"done_for_day"}),
}

#: Broker event name -> the status it carries. Closed; used only as a fallback
#: when the order body omits ``status``.
EVENT_TO_STATUS: dict[str, str] = {
    "new": "new",
    "accepted": "accepted",
    "partial_fill": "partially_filled",
    "fill": "filled",
    "canceled": "canceled",
    "rejected": "rejected",
    "expired": "expired",
    "replaced": "replaced",
    "done_for_day": "done_for_day",
}

#: Event names that carry an individual execution and must produce a `Fill`.
FILL_EVENTS: frozenset[str] = frozenset({"partial_fill", "fill"})


class _Bad(Exception):
    """A broker field that cannot be coerced to its declared type."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _opt_int(value: Any, code: str) -> int | None:
    """Coerce a broker integer (arrives as a string); ``None``/empty -> ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise _Bad(code)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not float(value).is_integer():
            raise _Bad(code)
        return int(value)
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001 - a hostile __str__ must not kill the stream
        raise _Bad(code) from None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        parsed = float(text)
    except ValueError:
        raise _Bad(code) from None
    if not math.isfinite(parsed) or not parsed.is_integer():
        raise _Bad(code)
    return int(parsed)


def _opt_float(value: Any, code: str) -> float | None:
    """Coerce a broker decimal (arrives as a string); ``None``/empty -> ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise _Bad(code)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise _Bad(code) from None
    if not math.isfinite(parsed):
        raise _Bad(code)
    return parsed


def _opt_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class OrderState:
    """The broker-confirmed state of one order (plan §4.4)."""

    client_order_id: str
    order_id: str | None
    symbol: str
    side: str
    qty: int
    filled_qty: int
    avg_fill_price: float | None
    status: str
    updated_at: str
    reason: str | None = None

    @property
    def open_qty(self) -> int:
        return self.qty - self.filled_qty

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def is_open(self) -> bool:
        return not self.is_terminal


@dataclass(frozen=True)
class Fill:
    """One execution, as reported by a ``fill``/``partial_fill`` event."""

    client_order_id: str
    order_id: str | None
    symbol: str
    side: str
    qty: int
    price: float
    at: str


class OrderStateManager:
    """Maintains one :class:`OrderState` per ``client_order_id`` from trade updates.

    Deterministic: events are applied in order, all iteration that reaches an
    output is sorted, and the only clock read is the injected ``now``.
    """

    def __init__(self, now: Callable[[], datetime], audit: AuditChain | None = None) -> None:
        self._now = now
        self._audit = audit
        self._states: dict[str, OrderState] = {}
        self._fills: list[Fill] = []
        self._anomalies: list[str] = []

    def apply(self, event: dict[str, Any]) -> OrderState | None:
        """Apply one ``trade_updates`` event; returns the resulting state.

        Returns ``None`` only when the event is unusable *and* no prior state
        exists (the anomaly is still recorded) - a valid event always yields a
        state, and a fill for an unknown ``client_order_id`` creates one.
        An anomalous event leaves the state untouched and never raises.
        """
        if not isinstance(event, dict):
            return self._anomaly("malformed_event", {"raw": repr(event)}, None)
        order = event.get("order")
        if not isinstance(order, dict):
            return self._anomaly("missing_order", event, None)
        cid = order.get("client_order_id")
        if not isinstance(cid, str) or not cid:
            return self._anomaly("missing_client_order_id", event, None)
        prev = self._states.get(cid)

        event_name = event.get("event")
        if event_name is not None and event_name not in EVENT_TO_STATUS:
            return self._anomaly("unknown_event", event, cid)
        target = self._target_status(order, event_name, event, cid)
        if target is None:
            return self._states.get(cid)

        symbol = order.get("symbol")
        side = order.get("side")
        if not isinstance(symbol, str) or not symbol:
            return self._anomaly("missing_symbol", event, cid)
        if not isinstance(side, str) or not side:
            return self._anomaly("missing_side", event, cid)

        try:
            qty = _opt_int(order.get("qty"), "bad_qty")
            broker_filled = _opt_int(order.get("filled_qty"), "bad_filled_qty")
            broker_avg = _opt_float(order.get("filled_avg_price"), "bad_filled_avg_price")
        except _Bad as bad:
            return self._anomaly(bad.code, event, cid)
        if qty is None:
            return self._anomaly("missing_qty", event, cid)

        if (
            prev is not None
            and target != prev.status
            and target not in LEGAL_TRANSITIONS[prev.status]
        ):
            return self._anomaly(
                "illegal_transition", event, cid, detail=f"{prev.status}->{target}"
            )

        order_id = _opt_text(order.get("id"))
        at = _opt_text(event.get("timestamp")) or self._now().isoformat(timespec="seconds")
        reason = _opt_text(order.get("reject_reason")) or _opt_text(event.get("reason"))
        base_filled = prev.filled_qty if prev is not None else 0
        base_avg = prev.avg_fill_price if prev is not None else None

        fill: Fill | None = None
        if event_name in FILL_EVENTS:
            try:
                fill, new_filled, new_avg = self._apply_fill(
                    event, cid, order_id, symbol, side, base_filled, base_avg,
                    broker_filled, broker_avg, at,
                )
            except _Bad as bad:
                return self._anomaly(bad.code, event, cid)
        else:
            if broker_filled is not None:
                if broker_filled < base_filled:
                    return self._anomaly("filled_qty_regressed", event, cid)
                if prev is not None and broker_filled != base_filled:
                    return self._anomaly("unexpected_filled_qty", event, cid)
                new_filled = broker_filled
            else:
                new_filled = base_filled
            new_avg = broker_avg if broker_avg is not None else base_avg

        if new_filled > qty:
            return self._anomaly("filled_qty_exceeds_qty", event, cid)

        state = OrderState(
            client_order_id=cid,
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            filled_qty=new_filled,
            avg_fill_price=new_avg,
            status=target,
            updated_at=at,
            reason=reason,
        )
        self._states[cid] = state
        if fill is not None:
            self._fills.append(fill)
        if self._audit is not None:
            if fill is not None:
                self._audit.append(
                    "order_fill",
                    "fill",
                    client_order_id=cid,
                    order_id=order_id,
                    symbol=symbol,
                    side=side,
                    qty=fill.qty,
                    price=fill.price,
                    at=fill.at,
                )
            self._audit.append(
                "order_state",
                target,
                client_order_id=cid,
                order_id=order_id,
                symbol=symbol,
                side=side,
                qty=qty,
                filled_qty=new_filled,
                avg_fill_price=new_avg,
                status=target,
            )
        return state

    def _target_status(
        self, order: dict[str, Any], event_name: Any, event: dict[str, Any], cid: str
    ) -> str | None:
        """The order body's status, falling back to the event's status mapping."""
        status = order.get("status")
        if status is None or status == "":
            if event_name is None:
                self._anomaly("missing_status", event, cid)
                return None
            return EVENT_TO_STATUS[event_name]
        if not isinstance(status, str) or status not in STATES:
            self._anomaly("unknown_status", event, cid)
            return None
        return status

    def _apply_fill(
        self,
        event: dict[str, Any],
        cid: str,
        order_id: str | None,
        symbol: str,
        side: str,
        base_filled: int,
        base_avg: float | None,
        broker_filled: int | None,
        broker_avg: float | None,
        at: str,
    ) -> tuple[Fill, int, float | None]:
        """Build the `Fill` and the advanced quantity/average.

        Raises :class:`_Bad` with the anomaly code for any unapplyable fill; the
        caller records it and leaves the state untouched.
        """
        fill_qty = _opt_int(event.get("qty"), "bad_fill_qty")
        fill_price = _opt_float(event.get("price"), "bad_fill_price")
        if fill_qty is None:
            if broker_filled is None:
                raise _Bad("missing_fill_qty")
            fill_qty = broker_filled - base_filled
        if fill_price is None:
            fill_price = broker_avg
        if fill_price is None:
            raise _Bad("missing_fill_price")
        if fill_qty <= 0:
            raise _Bad("non_positive_fill_qty")
        if broker_filled is not None:
            if broker_filled < base_filled:
                raise _Bad("filled_qty_regressed")
            if broker_filled != base_filled + fill_qty:
                raise _Bad("filled_qty_mismatch")
            new_filled = broker_filled
        else:
            new_filled = base_filled + fill_qty
        if broker_avg is not None:
            new_avg = broker_avg
        elif base_avg is None or base_filled == 0:
            new_avg = fill_price
        else:
            new_avg = (base_filled * base_avg + fill_qty * fill_price) / new_filled
        fill = Fill(cid, order_id, symbol, side, fill_qty, fill_price, at)
        return fill, new_filled, new_avg

    def _anomaly(
        self, code: str, raw: dict[str, Any], cid: str | None, detail: str = ""
    ) -> OrderState | None:
        """Record an unapplicable event with its raw body; return the untouched state."""
        text = code
        if detail:
            text += f" ({detail})"
        if cid is not None:
            text += f" client_order_id={cid!r}"
        text += " raw_event=" + json.dumps(raw, sort_keys=True, default=str)
        self._anomalies.append(text)
        if self._audit is not None:
            self._audit.append(
                "order_anomaly", code, client_order_id=cid, detail=detail, raw_event=raw
            )
        return self._states.get(cid) if cid is not None else None

    def state(self, client_order_id: str) -> OrderState | None:
        return self._states.get(client_order_id)

    def open_orders(self) -> tuple[OrderState, ...]:
        """Non-terminal orders, sorted by ``client_order_id``."""
        return tuple(
            sorted(
                (s for s in self._states.values() if not s.is_terminal),
                key=lambda s: s.client_order_id,
            )
        )

    def fills(self) -> tuple[Fill, ...]:
        """Every recorded execution, in arrival order."""
        return tuple(self._fills)

    def anomalies(self) -> tuple[str, ...]:
        """Every unapplied event, in arrival order (raw body included)."""
        return tuple(self._anomalies)

    def restore(self, states: Iterable[OrderState]) -> None:
        """Cold-start from the broker's own states, newest truth wins.

        States are applied in ``client_order_id`` order so the result does not
        depend on the broker's listing order. An inconsistent state is recorded
        as an anomaly and skipped rather than trusted.
        """
        for st in sorted(states, key=lambda s: s.client_order_id):
            if st.status not in STATES:
                self._anomaly(
                    "restore_unknown_status",
                    {"client_order_id": st.client_order_id, "status": st.status},
                    st.client_order_id,
                )
                continue
            if st.qty < 0 or st.filled_qty < 0 or st.filled_qty > st.qty:
                self._anomaly(
                    "restore_inconsistent_qty",
                    {
                        "client_order_id": st.client_order_id,
                        "qty": st.qty,
                        "filled_qty": st.filled_qty,
                    },
                    st.client_order_id,
                )
                continue
            self._states[st.client_order_id] = st
