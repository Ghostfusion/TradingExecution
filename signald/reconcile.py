"""Broker-vs-local reconciliation: the broker is the source of truth (plan §4.4 C21, design §13).

Invariant enforced here: reconciliation only *detects* contradiction - it never
"fixes" the local ledger, and it never assumes the broker is wrong. Any drift
sets ``halt=True`` so the caller stops submitting and the operator investigates
the fill stream (design §11.2). Exposure is reconstructed from **fills** (the
signed sum of quantity per symbol), never from targets or intended positions.

Inputs may be attribute-style objects (``obj.symbol`` / ``obj.qty`` /
``obj.client_order_id``) or plain dicts (``{"symbol": ..., "qty": ...,
"client_order_id": ...}``); both are accepted and neither is mutated. An input
that cannot be read is rejected with ``ValueError`` (the caller's gate turns an
exception into a ``BLOCK``, never a pass), and a cash balance known on only one
side is drift, not a pass.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: Broker order statuses that mean the order is no longer working. Anything else
#: (including an unknown/missing status) is treated as open - fail closed.
CLOSED_ORDER_STATUSES = frozenset(
    {
        "filled",
        "canceled",
        "cancelled",
        "expired",
        "rejected",
        "done_for_day",
        "stopped",
        "suspended",
        "replaced",
        "not_found",
    }
)


@dataclass(frozen=True)
class Drift:
    """One contradiction, carrying both numbers so the operator sees which side is wrong."""

    kind: str  # "position" | "order" | "fill" | "cash"
    symbol: str | None
    local: Any
    broker: Any
    detail: str


@dataclass(frozen=True)
class ReconcileReport:
    """Outcome of one reconciliation pass: an ``ok`` report has no drift and no halt."""

    ok: bool
    halt: bool
    drift: tuple[Drift, ...]
    detail: str
    checked_at: str


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _net_by_symbol(records: Iterable[Any], label: str) -> dict[str, float]:
    """Sum signed quantity per symbol; a record without symbol/qty is rejected."""
    net: dict[str, float] = {}
    for rec in records:
        symbol = _field(rec, "symbol")
        qty = _field(rec, "qty")
        if symbol is None or qty is None:
            raise ValueError(f"{label} record must expose symbol and qty, got {rec!r}")
        net[str(symbol)] = net.get(str(symbol), 0.0) + float(qty)
    return net


def _client_order_id(obj: Any) -> str | None:
    cid = _field(obj, "client_order_id")
    return None if cid is None else str(cid)


def _position_drift(
    symbol: str, local_qty: float, broker_qty: float, tolerance: float
) -> Drift:
    detail = (
        f"{symbol}: local fills {local_qty:g} sh vs broker {broker_qty:g} sh "
        f"(tolerance {tolerance:g})"
    )
    return Drift("position", symbol, local_qty, broker_qty, detail)


def reconcile(
    *,
    local_fills: Iterable,
    broker_positions: Iterable,
    broker_orders: Iterable = (),
    local_open_orders: Iterable = (),
    cash_local: float | None = None,
    cash_broker: float | None = None,
    now: datetime,
    share_tolerance: float = 0.0,
    cash_tolerance: float = 1.0,
) -> ReconcileReport:
    """Compare the local ledger (from fills) with the broker and report drift.

    ``local_fills`` are the local fill stream (signed quantity per fill);
    positions are the per-symbol sum of those fills. ``broker_positions`` are the
    broker's reported positions. ``broker_orders`` is the broker's order list -
    any order still working that our ``local_open_orders`` (matched on
    ``client_order_id``) does not know is drift. ``cash_local``/``cash_broker``
    are compared only when both are supplied.
    """
    drift: list[Drift] = []

    local_net = _net_by_symbol(local_fills, "fill")
    broker_net = _net_by_symbol(broker_positions, "position")

    for symbol in sorted(set(local_net) | set(broker_net)):
        local_qty = local_net.get(symbol, 0.0)
        broker_qty = broker_net.get(symbol, 0.0)
        if abs(local_qty - broker_qty) > share_tolerance:
            drift.append(_position_drift(symbol, local_qty, broker_qty, share_tolerance))

    known_ids = {cid for cid in map(_client_order_id, local_open_orders) if cid is not None}
    for order in broker_orders:
        status = str(_field(order, "status", "") or "").lower()
        if status in CLOSED_ORDER_STATUSES:
            continue
        cid = _client_order_id(order)
        if cid is None:
            drift.append(
                Drift(
                    "order",
                    _field(order, "symbol"),
                    None,
                    _field(order, "status"),
                    "broker has a working order with no client_order_id: cannot be matched",
                )
            )
        elif cid not in known_ids:
            drift.append(
                Drift(
                    "order",
                    _field(order, "symbol"),
                    None,
                    {"client_order_id": cid, "status": status or None},
                    f"broker order {cid} is working but unknown locally",
                )
            )

    if cash_local is not None or cash_broker is not None:
        if cash_local is None or cash_broker is None:
            drift.append(
                Drift(
                    "cash",
                    None,
                    cash_local,
                    cash_broker,
                    "cash available on only one side: cannot reconcile",
                )
            )
        elif abs(float(cash_local) - float(cash_broker)) > cash_tolerance:
            drift.append(
                Drift(
                    "cash",
                    None,
                    cash_local,
                    cash_broker,
                    f"cash local {cash_local:,.2f} vs broker {cash_broker:,.2f} "
                    f"(tolerance {cash_tolerance:,.2f})",
                )
            )

    drift.sort(key=lambda d: (d.kind, d.symbol or ""))
    ordered = tuple(drift)
    ok = not ordered
    detail = (
        "in sync"
        if ok
        else f"{len(ordered)} drift(s): " + "; ".join(d.detail for d in ordered)
    )
    return ReconcileReport(
        ok=ok,
        halt=not ok,
        drift=ordered,
        detail=detail,
        checked_at=now.isoformat(),
    )
