"""Order manager: own-before-write, query-before-retry, no blind resend (plan §4.4).

The recovery invariant is exercised end to end: the pending row is read from
disk *inside* the fake transport (proving it was written first), and every
uncertain outcome is shown to query by ``client_order_id`` before any resubmit.
No network, no wall clock: the transport is a plain recorder and time is the
``now`` fixture.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from signald.order.manager import OrderManager
from signald.stores import AuditChain

pytestmark = pytest.mark.timeout(120)

ACCEPTED = {
    "id": "ord-1",
    "client_order_id": "cid-1",
    "status": "accepted",
    "qty": "10",
    "filled_qty": "0",
}
NOT_FOUND = {"id": None, "status": "not_found"}
DUPLICATE = {"error": {"code": 422, "message": "client_order_id must be unique"}}


@dataclass(frozen=True)
class FakeIntent:
    """Stand-in for guard.OrderIntent, matching the frozen §2.4 field contract."""

    intent_id: str = "int-1"
    sleeve: str = "intraday"
    symbol: str = "NVDA"
    side: str = "buy"
    qty: int = 10
    order_type: str = "limit"
    limit_price: float | None = 100.0
    stop_price: float | None = 95.0
    tif: str = "day"
    client_order_id: str = "cid-1"


class FakeTransport:
    """Records calls; returns one scripted response per method (raises to inject faults)."""

    def __init__(self, probe=None, **responses):
        self.calls: list[tuple[str, dict]] = []
        self._queues = {method: list(bodies) for method, bodies in responses.items()}
        self._probe = probe

    def __call__(self, method, payload):
        self.calls.append((method, dict(payload)))
        if self._probe is not None:
            self._probe(method, payload)
        item = self._queues[method].pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def make_manager(tmp_path, transport, now, audit=None) -> OrderManager:
    chain = audit if audit is not None else AuditChain(tmp_path / "audit.jsonl", lambda: now)
    return OrderManager(tmp_path / "pending.jsonl", transport, lambda: now, chain)


def kinds(chain: AuditChain) -> list[str]:
    return [row["kind"] for row in chain.read()]


# --- happy path ------------------------------------------------------------
def test_happy_submit_writes_the_pending_row_then_returns_the_broker_body(tmp_path, now):
    transport = FakeTransport(submit=[ACCEPTED])
    chain = AuditChain(tmp_path / "audit.jsonl", lambda: now)
    manager = make_manager(tmp_path, transport, now, chain)

    result = manager.submit(FakeIntent())

    assert result.status == "submitted"
    assert result.client_order_id == "cid-1"
    assert result.order == ACCEPTED
    assert result.retried is False
    assert transport.calls[0] == (
        "submit",
        {
            "client_order_id": "cid-1",
            "symbol": "NVDA",
            "side": "buy",
            "qty": 10,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": 100.0,
            "stop_price": 95.0,
        },
    )
    assert manager.pending() == [
        {
            "client_order_id": "cid-1",
            "intent_id": "int-1",
            "symbol": "NVDA",
            "side": "buy",
            "qty": 10,
            "status": "pending",
            "at": now.isoformat(),
        }
    ]
    assert "order_submitted" in kinds(chain)
    assert chain.verify()[0]


def test_pending_row_is_on_disk_before_the_transport_is_called(tmp_path, now):
    seen: dict[str, list[dict]] = {}

    def probe(method, payload):
        seen[method] = manager.pending()

    transport = FakeTransport(probe=probe, submit=[ACCEPTED])
    manager = make_manager(tmp_path, transport, now)

    manager.submit(FakeIntent())

    assert [row["client_order_id"] for row in seen["submit"]] == ["cid-1"]


# --- failures never become a pass -----------------------------------------
def test_transport_returning_none_is_unavailable_and_keeps_the_row(tmp_path, now):
    transport = FakeTransport(submit=[None])
    manager = make_manager(tmp_path, transport, now)

    result = manager.submit(FakeIntent())

    assert result.status == "unavailable"
    assert result.order is None
    assert len(manager.pending()) == 1


def test_transport_raising_timeout_is_a_timeout_and_keeps_the_row(tmp_path, now):
    transport = FakeTransport(submit=[TimeoutError("slow")])
    manager = make_manager(tmp_path, transport, now)

    result = manager.submit(FakeIntent())

    assert result.status == "timeout"
    assert len(manager.pending()) == 1


@pytest.mark.parametrize("body", [{"id": "ord-1", "status": "weird"}, {}, "not-a-dict"])
def test_unrecognised_broker_answer_fails_closed(tmp_path, now, body):
    transport = FakeTransport(submit=[body])
    manager = make_manager(tmp_path, transport, now)

    result = manager.submit(FakeIntent())

    assert result.status == "unavailable"
    assert len(manager.pending()) == 1


# --- duplicate -------------------------------------------------------------
def test_duplicate_is_reported_and_never_retried(tmp_path, now):
    transport = FakeTransport(submit=[DUPLICATE, DUPLICATE, DUPLICATE])
    manager = make_manager(tmp_path, transport, now)

    result = manager.submit_with_retry(FakeIntent())

    assert result.status == "duplicate"
    assert result.retried is False
    assert transport.methods() == ["submit"]
    assert len(manager.pending()) == 1


# --- query before retry ----------------------------------------------------
def test_retry_queries_before_resubmitting_and_reports_retried(tmp_path, now):
    transport = FakeTransport(submit=[None, ACCEPTED], query=[NOT_FOUND])
    manager = make_manager(tmp_path, transport, now)

    result = manager.submit_with_retry(FakeIntent())

    assert result.status == "submitted"
    assert result.retried is True
    assert transport.methods() == ["submit", "query", "submit"]


def test_a_query_that_finds_the_order_prevents_a_second_submit(tmp_path, now):
    transport = FakeTransport(submit=[None], query=[ACCEPTED])
    manager = make_manager(tmp_path, transport, now)

    result = manager.submit_with_retry(FakeIntent())

    assert result.status == "submitted"
    assert result.retried is True
    assert result.order == ACCEPTED
    assert transport.methods() == ["submit", "query"]


def test_a_query_that_is_unavailable_never_resubmits(tmp_path, now):
    transport = FakeTransport(submit=[None], query=[None])
    manager = make_manager(tmp_path, transport, now)

    result = manager.submit_with_retry(FakeIntent())

    assert result.status == "unavailable"
    assert result.retried is True
    assert transport.methods() == ["submit", "query"]
    assert len(manager.pending()) == 1


def test_exhausted_attempts_return_the_last_result(tmp_path, now):
    transport = FakeTransport(submit=[None, None], query=[NOT_FOUND])
    manager = make_manager(tmp_path, transport, now)

    result = manager.submit_with_retry(FakeIntent(), attempts=2)

    assert result.status == "unavailable"
    assert result.retried is True
    assert transport.methods() == ["submit", "query", "submit"]


# --- rejection -------------------------------------------------------------
@pytest.mark.parametrize("status", ["rejected", "canceled", "expired"])
def test_rejection_is_terminal_resolved_and_audited_with_the_broker_reason(tmp_path, now, status):
    body = {"id": "ord-1", "status": status, "reason": "insufficient_buying_power"}
    transport = FakeTransport(submit=[body])
    chain = AuditChain(tmp_path / "audit.jsonl", lambda: now)
    manager = make_manager(tmp_path, transport, now, chain)

    result = manager.submit(FakeIntent())

    assert result.status == "rejected"
    assert result.detail == "insufficient_buying_power"
    assert manager.pending() == []
    rejected = [row for row in chain.read() if row["kind"] == "order_rejected"]
    assert rejected[-1]["reason"] == "insufficient_buying_power"
    assert rejected[-1]["data"]["client_order_id"] == "cid-1"
    assert "order_resolved" in kinds(chain)
    assert chain.verify()[0]


# --- cancel ----------------------------------------------------------------
def test_cancel_happy_path_resolves_the_row(tmp_path, now):
    transport = FakeTransport(
        submit=[ACCEPTED], cancel=[{"id": "ord-1", "status": "canceled"}]
    )
    chain = AuditChain(tmp_path / "audit.jsonl", lambda: now)
    manager = make_manager(tmp_path, transport, now, chain)
    manager.submit(FakeIntent())

    result = manager.cancel("cid-1")

    assert result.status == "canceled"
    assert result.order == {"id": "ord-1", "status": "canceled"}
    assert manager.pending() == []
    assert "order_canceled" in kinds(chain)
    assert chain.verify()[0]


def test_cancel_of_a_missing_order_reports_not_found_and_resolves(tmp_path, now):
    transport = FakeTransport(submit=[ACCEPTED], cancel=[NOT_FOUND])
    manager = make_manager(tmp_path, transport, now)
    manager.submit(FakeIntent())

    result = manager.cancel("cid-1")

    assert result.status == "not_found"
    assert manager.pending() == []


# --- replace ---------------------------------------------------------------
def test_replace_happy_path(tmp_path, now):
    transport = FakeTransport(
        submit=[ACCEPTED],
        replace=[{"id": "ord-1", "client_order_id": "cid-1", "status": "accepted"}],
    )
    chain = AuditChain(tmp_path / "audit.jsonl", lambda: now)
    manager = make_manager(tmp_path, transport, now, chain)
    manager.submit(FakeIntent())

    result = manager.replace("cid-1", qty=5, limit_price=101.5)

    assert result.status == "replaced"
    assert transport.calls[-1] == (
        "replace",
        {"client_order_id": "cid-1", "qty": 5, "limit_price": 101.5},
    )
    assert "order_replaced" in kinds(chain)
    assert chain.verify()[0]


def test_replace_requires_a_field_to_change(tmp_path, now):
    manager = make_manager(tmp_path, FakeTransport(), now)
    with pytest.raises(ValueError):
        manager.replace("cid-1")


# --- pending bookkeeping ---------------------------------------------------
def test_resolve_retires_the_pending_row(tmp_path, now):
    transport = FakeTransport(submit=[ACCEPTED])
    manager = make_manager(tmp_path, transport, now)
    manager.submit(FakeIntent())
    assert len(manager.pending()) == 1

    manager.resolve("cid-1", "filled")

    assert manager.pending() == []


def test_pending_starts_empty(tmp_path, now):
    manager = make_manager(tmp_path, FakeTransport(), now)
    assert manager.pending() == []
