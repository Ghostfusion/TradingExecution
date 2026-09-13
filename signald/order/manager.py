"""Order manager: own-before-write, query-before-retry, never a blind resend.

The invariant (plan §4.4, design §13 "recovery by exact ``client_order_id``"):
the pending row is on disk *before* the broker is called, so a crash between
the two leaves a recoverable record; and after any outcome that is not a
definitive broker answer the manager **queries by ``client_order_id`` before
it ever resubmits**. A transport returning ``None``, raising, or answering
with a status the manager does not recognise is an *unavailable* broker - a
failure, never a pass - and the pending row stays for the recovery sweep.

This module owns the order-transport seam (:data:`OrderTransport`) and the
pending store; it does not derive quantities, prices or stops (the policy,
gate and sizer own those) and it does not process fills (the state machine
and reconciler do).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..stores import _atomic_append

if TYPE_CHECKING:  # stands alone when the sibling guard lags
    from .guard import OrderIntent

#: Broker seam: ``transport(method, payload) -> body | None``. ``method`` is one
#: of submit/query/cancel/replace; ``None`` means *unavailable*, never success.
OrderTransport = Callable[[str, dict[str, Any]], "dict[str, Any] | None"]

#: Broker statuses that mean the order exists and is being worked.
_WORKING = frozenset({"accepted", "new", "pending_new", "submitted", "partially_filled"})
#: Broker statuses that are a refusal; the machine reason is audited verbatim.
_REJECTED = frozenset({"rejected", "canceled", "expired", "stopped", "suspended"})
#: Replace acceptances (the broker may echo either spelling).
_REPLACED = _WORKING | {"replaced", "pending_replace"}


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of one manager action, with the broker body when there was one."""

    status: str  # submitted | duplicate | rejected | timeout | not_found | unavailable
    client_order_id: str
    order: dict[str, Any] | None
    detail: str
    retried: bool = False


def _broker_status(body: Any) -> str | None:
    """Normalised broker status, or ``None`` when the body is not a mapping."""
    if not isinstance(body, dict):
        return None
    status = body.get("status")
    text = str(status).strip().lower() if status is not None else ""
    return text or None


def _reason(body: dict[str, Any]) -> str:
    """The broker's machine reason, preferring a structured error message."""
    err = body.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    for key in ("reject_reason", "reason", "detail"):
        if body.get(key):
            return str(body[key])
    return str(body.get("status") or "unknown")


def _is_duplicate(body: Any) -> bool:
    """A duplicate ``client_order_id`` is 422 (Alpaca); never a retry trigger."""
    if not isinstance(body, dict):
        return False
    err = body.get("error")
    if isinstance(err, dict):
        if err.get("code") == 422:
            return True
        message = str(err.get("message", "")).lower()
        return "client_order_id" in message and "unique" in message
    return False


def _submit_payload(intent: OrderIntent) -> dict[str, Any]:
    """Broker payload for one intent; entries are limit, never market."""
    payload: dict[str, Any] = {
        "client_order_id": str(intent.client_order_id),
        "symbol": intent.symbol,
        "side": intent.side,
        "qty": intent.qty,
        "type": getattr(intent, "order_type", None) or "limit",
        "time_in_force": getattr(intent, "tif", None) or "day",
    }
    limit_price = getattr(intent, "limit_price", None)
    if limit_price is not None:
        payload["limit_price"] = limit_price
    stop_price = getattr(intent, "stop_price", None)
    if stop_price is not None:
        payload["stop_price"] = stop_price
    return payload


class OrderManager:
    """Own the pending store and the broker seam for one session's orders."""

    def __init__(
        self,
        pending_path: str | Path,
        transport: OrderTransport,
        now: Callable[[], datetime],
        audit: Any | None = None,
    ) -> None:
        self._pending_path = Path(pending_path)
        self._transport = transport
        self._now = now
        self._audit_chain = audit

    # -- transport seam ----------------------------------------------------
    def _call(self, method: str, payload: dict[str, Any]) -> tuple[dict | None, str | None]:
        """Invoke the seam; return ``(body, failure)`` with failure a status or None.

        ``None`` from the transport, or any exception it raises, is an
        unavailable broker (fail closed); a ``TimeoutError`` is reported as a
        timeout so the retry path may query before resubmitting.
        """
        try:
            return self._transport(method, payload), None
        except TimeoutError:
            return None, "timeout"
        except Exception:  # noqa: BLE001 - any transport fault is an unavailable broker
            return None, "unavailable"

    # -- public surface ----------------------------------------------------
    def submit(self, intent: OrderIntent) -> SubmitResult:
        """Write the pending row, then submit; classify the broker's answer."""
        cid = str(intent.client_order_id)
        self._append_pending(intent)
        body, failure = self._call("submit", _submit_payload(intent))
        if failure is not None:
            return self._unavailable(failure, cid, f"transport {failure} on submit", intent)
        if body is None:
            return self._unavailable("unavailable", cid, "transport returned no body", intent)
        if not isinstance(body, dict):
            return self._unavailable(
                "unavailable", cid, f"non-mapping broker body {type(body).__name__}", intent
            )
        status = _broker_status(body)
        if _is_duplicate(body):
            self._audit(
                "order_duplicate",
                "client_order_id already exists at the broker",
                client_order_id=cid,
                intent_id=intent.intent_id,
            )
            return SubmitResult("duplicate", cid, body, _reason(body))
        if status == "not_found":
            return self._unavailable(
                "not_found", cid, "broker reported not_found on submit", intent, body=body
            )
        if status in _WORKING:
            self._audit(
                "order_submitted",
                "accepted by the broker",
                client_order_id=cid,
                intent_id=intent.intent_id,
            )
            return SubmitResult("submitted", cid, body, "accepted")
        if status in _REJECTED:
            reason = _reason(body)
            self.resolve(cid, "rejected")
            self._audit(
                "order_rejected",
                reason,
                client_order_id=cid,
                intent_id=intent.intent_id,
                broker_status=status,
            )
            return SubmitResult("rejected", cid, body, reason)
        return self._unavailable(
            "unavailable", cid, f"unknown broker status {status!r}", intent, body=body
        )

    def submit_with_retry(self, intent: OrderIntent, *, attempts: int = 3) -> SubmitResult:
        """Submit, then recover by query before any resubmit (design §13).

        The only case that resubmits is a query that reports ``not_found``; a
        query that finds the order recovers it, and a query that is itself
        unavailable stops the loop with ``unavailable``.
        """
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        result = self.submit(intent)
        retried = False
        for _ in range(attempts - 1):
            if result.status not in ("unavailable", "timeout"):
                return replace(result, retried=retried)
            cid = result.client_order_id
            found = self.query(cid)
            retried = True
            if found is None:
                self._audit(
                    "order_unavailable",
                    f"query after {result.status} was unavailable; not resubmitting",
                    client_order_id=cid,
                    intent_id=intent.intent_id,
                )
                return SubmitResult(
                    "unavailable",
                    cid,
                    None,
                    f"query after {result.status} was unavailable",
                    retried=True,
                )
            if _broker_status(found) == "not_found":
                self._audit(
                    "order_retried",
                    "query reported not_found; resubmitting",
                    client_order_id=cid,
                    intent_id=intent.intent_id,
                )
                result = self.submit(intent)
                continue
            self._audit(
                "order_retried",
                "query found the order; recovered without resubmitting",
                client_order_id=cid,
                intent_id=intent.intent_id,
            )
            return SubmitResult(
                "submitted", cid, found, "recovered by client_order_id", retried=True
            )
        return replace(result, retried=True)

    def query(self, client_order_id: str) -> dict[str, Any] | None:
        """The broker's view of one order, or ``None`` when unavailable."""
        body, failure = self._call("query", {"client_order_id": str(client_order_id)})
        if failure is not None or not isinstance(body, dict):
            return None
        return body

    def cancel(self, client_order_id: str) -> SubmitResult:
        """Cancel one order; a not_found resolves the pending row, not an error."""
        cid = str(client_order_id)
        body, failure = self._call("cancel", {"client_order_id": cid})
        if failure is not None or not isinstance(body, dict):
            return self._unavailable(
                failure or "unavailable", cid, "cancel transport unavailable"
            )
        status = _broker_status(body)
        if status == "not_found":
            self.resolve(cid, "not_found")
            return SubmitResult("not_found", cid, body, "no such order to cancel")
        if status == "canceled":
            self.resolve(cid, "canceled")
            self._audit("order_canceled", "canceled at the broker", client_order_id=cid)
            return SubmitResult("canceled", cid, body, "canceled")
        if status in _REJECTED:
            reason = _reason(body)
            self._audit("order_rejected", reason, client_order_id=cid, broker_status=status)
            return SubmitResult("rejected", cid, body, reason)
        return self._unavailable(
            "unavailable", cid, f"unknown cancel status {status!r}", body=body
        )

    def replace(
        self,
        client_order_id: str,
        *,
        qty: int | None = None,
        limit_price: float | None = None,
    ) -> SubmitResult:
        """Replace qty/price on a working order; at least one field is required."""
        if qty is None and limit_price is None:
            raise ValueError("replace requires qty or limit_price")
        cid = str(client_order_id)
        payload: dict[str, Any] = {"client_order_id": cid}
        if qty is not None:
            payload["qty"] = qty
        if limit_price is not None:
            payload["limit_price"] = limit_price
        body, failure = self._call("replace", payload)
        if failure is not None or not isinstance(body, dict):
            return self._unavailable(
                failure or "unavailable", cid, "replace transport unavailable"
            )
        status = _broker_status(body)
        if status == "not_found":
            return SubmitResult("not_found", cid, body, "no such order to replace")
        if status in _REJECTED:
            reason = _reason(body)
            self._audit("order_rejected", reason, client_order_id=cid, broker_status=status)
            return SubmitResult("rejected", cid, body, reason)
        if status in _REPLACED:
            self._audit("order_replaced", "replaced at the broker", client_order_id=cid)
            return SubmitResult("replaced", cid, body, "replaced")
        return self._unavailable(
            "unavailable", cid, f"unknown replace status {status!r}", body=body
        )

    def pending(self) -> list[dict[str, Any]]:
        """Rows still awaiting a terminal outcome, oldest first (recovery sweep)."""
        live: dict[str, dict[str, Any]] = {}
        for row in self._read_rows():
            cid = row.get("client_order_id")
            if not cid:
                continue
            key = str(cid)
            if row.get("status") == "pending" and "intent_id" in row:
                live[key] = row
            else:
                live.pop(key, None)  # a resolution row retires the pending row
        return [live[k] for k in sorted(live, key=lambda c: (str(live[c].get("at", "")), c))]

    def resolve(self, client_order_id: str, status: str) -> None:
        """Retire a pending row (append-only) and audit the resolution."""
        cid = str(client_order_id)
        row = {
            "client_order_id": cid,
            "status": str(status),
            "at": self._now().isoformat(timespec="seconds"),
        }
        _atomic_append(self._pending_path, json.dumps(row, sort_keys=True) + "\n")
        self._audit("order_resolved", f"resolved as {status}", client_order_id=cid, status=status)

    # -- internals ---------------------------------------------------------
    def _append_pending(self, intent: OrderIntent) -> None:
        """Own-before-write: the row is durable before the broker is contacted."""
        row = {
            "client_order_id": str(intent.client_order_id),
            "intent_id": str(intent.intent_id),
            "symbol": intent.symbol,
            "side": intent.side,
            "qty": intent.qty,
            "status": "pending",
            "at": self._now().isoformat(timespec="seconds"),
        }
        _atomic_append(self._pending_path, json.dumps(row, sort_keys=True) + "\n")

    def _read_rows(self) -> list[dict[str, Any]]:
        if not self._pending_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self._pending_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _unavailable(
        self,
        status: str,
        cid: str,
        detail: str,
        intent: OrderIntent | None = None,
        *,
        body: dict[str, Any] | None = None,
    ) -> SubmitResult:
        data: dict[str, Any] = {"client_order_id": cid, "broker_status": status}
        if intent is not None:
            data["intent_id"] = intent.intent_id
        self._audit("order_unavailable", detail, **data)
        return SubmitResult(status, cid, body, detail)

    def _audit(self, kind: str, reason: str, **data: Any) -> None:
        if self._audit_chain is not None:
            self._audit_chain.append(kind, reason, **data)
