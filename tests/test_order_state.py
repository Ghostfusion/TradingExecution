"""Order state machine tests (plan §4.4, §9.2 golden lifecycle files).

Every test drives the manager with recorded-shape ``trade_updates`` bodies - no
network, no wall clock (events carry their own timestamp, the injected clock is
only the fallback).
"""

from __future__ import annotations

import pytest

from signald.order.state import (
    EVENT_TO_STATUS,
    LEGAL_TRANSITIONS,
    STATES,
    Fill,
    OrderState,
    OrderStateManager,
)
from signald.stores import AuditChain

pytestmark = pytest.mark.timeout(120)

TS = "2026-09-12T13:45:02Z"


def _update(
    event: str,
    *,
    cid: str = "cid-1",
    oid: str = "ord-1",
    symbol: str = "AAPL",
    side: str = "buy",
    qty: str = "10",
    filled_qty: str = "0",
    filled_avg_price: str | None = None,
    status: str | None = None,
    price: str | None = None,
    fill_qty: str | None = None,
    ts: str | None = TS,
) -> dict:
    """Build one Alpaca-shaped ``trade_updates`` event (numbers as strings)."""
    order: dict = {
        "id": oid,
        "client_order_id": cid,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "status": status if status is not None else event,
    }
    ev: dict = {"event": event, "order": order}
    if ts is not None:
        ev["timestamp"] = ts
    if price is not None:
        ev["price"] = price
    if fill_qty is not None:
        ev["qty"] = fill_qty
    return ev


@pytest.fixture
def audit(cfg):
    return AuditChain(cfg.audit_file, cfg.now)


@pytest.fixture
def mgr(cfg, audit):
    return OrderStateManager(now=cfg.now, audit=audit)


def test_states_is_the_closed_set():
    assert STATES == (
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


def test_transition_table_covers_only_known_states():
    assert set(LEGAL_TRANSITIONS) == set(STATES)
    for targets in LEGAL_TRANSITIONS.values():
        assert targets <= set(STATES)
    assert set(EVENT_TO_STATUS.values()) == set(STATES)


def test_golden_lifecycle_with_hand_worked_weighted_average(mgr):
    first = mgr.apply(_update("new"))
    assert first is not None
    assert first.status == "new"
    assert first.is_open and not first.is_terminal
    assert first.open_qty == 10

    accepted = mgr.apply(_update("accepted"))
    assert accepted is not None
    assert accepted.status == "accepted"

    # Partial 4 @ 10.00. Our weighted average is used because the broker body
    # carries no filled_avg_price in this fixture.
    partial = mgr.apply(
        _update(
            "partial_fill", status="partially_filled", fill_qty="4",
            price="10.00", filled_qty="4",
        )
    )
    assert partial is not None
    assert partial.filled_qty == 4
    assert partial.avg_fill_price == pytest.approx(10.00)

    # Partial 2 @ 13.00 -> (4*10.00 + 2*13.00) / 6 = 66/6 = 11.00.
    partial = mgr.apply(
        _update(
            "partial_fill", status="partially_filled", fill_qty="2",
            price="13.00", filled_qty="6",
        )
    )
    assert partial is not None
    assert partial.filled_qty == 6
    assert partial.avg_fill_price == pytest.approx(11.00)

    # Final 4 @ 11.50 -> (6*11.00 + 4*11.50) / 10 = 112/10 = 11.20.
    final = mgr.apply(
        _update("fill", status="filled", fill_qty="4", price="11.50", filled_qty="10")
    )
    assert final is not None
    assert final.status == "filled"
    assert final.filled_qty == 10
    assert final.avg_fill_price == pytest.approx(11.20)
    assert final.open_qty == 0
    assert final.is_terminal and not final.is_open

    fills = mgr.fills()
    assert [(f.qty, f.price) for f in fills] == [(4, 10.00), (2, 13.00), (4, 11.50)]
    assert all(isinstance(f, Fill) and f.client_order_id == "cid-1" for f in fills)
    assert all(f.symbol == "AAPL" and f.side == "buy" and f.at == TS for f in fills)
    assert mgr.anomalies() == ()


def test_broker_filled_avg_price_is_authoritative_when_present(mgr):
    mgr.apply(_update("accepted"))
    mgr.apply(
        _update(
            "partial_fill", status="partially_filled", fill_qty="4", price="10.00",
            filled_qty="4", filled_avg_price="10.00",
        )
    )
    # The broker's cumulative average wins over a naive (4*10 + 6*11)/10.
    state = mgr.apply(
        _update(
            "partial_fill", status="partially_filled", fill_qty="6", price="11.00",
            filled_qty="10", filled_avg_price="10.60",
        )
    )
    assert state is not None
    assert state.filled_qty == 10
    assert state.avg_fill_price == pytest.approx(10.60)


@pytest.mark.parametrize(
    "event,status",
    [
        ("rejected", "rejected"),
        ("canceled", "canceled"),
        ("expired", "expired"),
        ("replaced", "replaced"),
        ("done_for_day", "done_for_day"),
    ],
)
def test_terminal_events_end_the_order_and_leave_open_orders(mgr, event, status):
    mgr.apply(_update("accepted"))
    state = mgr.apply(_update(event, status=status))
    assert state is not None
    assert state.status == status
    assert state.is_terminal and not state.is_open
    assert mgr.open_orders() == ()


def test_late_event_after_a_terminal_status_is_an_anomaly_and_does_not_move_state(mgr):
    mgr.apply(_update("accepted"))
    terminal = mgr.apply(_update("rejected", status="rejected"))
    out = mgr.apply(_update("accepted", status="accepted"))
    assert out is terminal  # the same frozen instance, untouched
    assert mgr.state("cid-1").status == "rejected"
    assert len(mgr.anomalies()) == 1
    assert "illegal_transition" in mgr.anomalies()[0]


def test_illegal_transition_is_an_anomaly_and_does_not_move_state(mgr):
    mgr.apply(_update("accepted"))
    partial = mgr.apply(
        _update(
            "partial_fill", status="partially_filled", fill_qty="4",
            price="10.00", filled_qty="4",
        )
    )
    # A partially filled order can never be rejected.
    out = mgr.apply(_update("rejected", status="rejected"))
    assert out is partial
    assert mgr.state("cid-1").status == "partially_filled"
    assert len(mgr.anomalies()) == 1


def test_fill_for_unknown_client_order_id_creates_the_state(mgr):
    state = mgr.apply(
        _update("fill", cid="cid-9", oid="ord-9", status="filled", fill_qty="10",
                price="12.34", filled_qty="10")
    )
    assert state is not None
    assert state.client_order_id == "cid-9"
    assert state.order_id == "ord-9"
    assert state.filled_qty == 10
    assert state.avg_fill_price == pytest.approx(12.34)
    assert state.is_terminal
    assert mgr.anomalies() == ()
    assert len(mgr.fills()) == 1


def test_string_typed_numbers_coerce(mgr):
    state = mgr.apply(
        _update("fill", status="filled", fill_qty="10", price="12.34",
                filled_qty="10", filled_avg_price="12.34")
    )
    assert state is not None
    assert isinstance(state.qty, int) and state.qty == 10
    assert isinstance(state.filled_qty, int) and state.filled_qty == 10
    assert isinstance(state.avg_fill_price, float)
    assert state.avg_fill_price == pytest.approx(12.34)


def test_non_numeric_fill_qty_is_an_anomaly_without_raising(mgr):
    accepted = mgr.apply(_update("accepted"))
    bad = _update("partial_fill", status="partially_filled", fill_qty="abc", price="10.00",
                  filled_qty="4")
    out = mgr.apply(bad)
    assert out is accepted
    assert mgr.state("cid-1").status == "accepted"
    assert mgr.state("cid-1").filled_qty == 0
    assert len(mgr.anomalies()) == 1
    assert "bad_fill_qty" in mgr.anomalies()[0]


def test_non_numeric_qty_on_a_fresh_order_is_an_anomaly_without_raising(mgr):
    assert mgr.apply(_update("fill", status="filled", qty="abc", fill_qty="10",
                             price="10.00", filled_qty="10")) is None
    assert mgr.state("cid-1") is None
    assert len(mgr.fills()) == 0
    assert len(mgr.anomalies()) == 1
    assert "bad_qty" in mgr.anomalies()[0]


def test_non_numeric_filled_qty_is_an_anomaly(mgr):
    mgr.apply(_update("accepted"))
    out = mgr.apply(_update("partial_fill", status="partially_filled", fill_qty="4",
                            price="10.00", filled_qty="four"))
    assert out.status == "accepted"
    assert len(mgr.anomalies()) == 1
    assert "bad_filled_qty" in mgr.anomalies()[0]


def test_filled_qty_greater_than_qty_is_an_anomaly(mgr):
    mgr.apply(_update("accepted"))
    out = mgr.apply(_update("fill", status="filled", fill_qty="11", price="10.00",
                            filled_qty="11"))
    assert out.status == "accepted"
    assert out.filled_qty == 0
    assert len(mgr.anomalies()) == 1
    assert "filled_qty_exceeds_qty" in mgr.anomalies()[0]


def test_cumulative_filled_qty_that_disagrees_with_the_fill_is_an_anomaly(mgr):
    mgr.apply(_update("accepted"))
    out = mgr.apply(_update("partial_fill", status="partially_filled", fill_qty="4",
                            price="10.00", filled_qty="7"))
    assert out.status == "accepted"
    assert len(mgr.anomalies()) == 1
    assert "filled_qty_mismatch" in mgr.anomalies()[0]


def test_filled_qty_regression_is_an_anomaly(mgr):
    mgr.apply(_update("accepted"))
    mgr.apply(_update("partial_fill", status="partially_filled", fill_qty="4",
                      price="10.00", filled_qty="4"))
    out = mgr.apply(_update("canceled", status="canceled", filled_qty="2"))
    assert out.status == "partially_filled"
    assert out.filled_qty == 4
    assert len(mgr.anomalies()) == 1
    assert "filled_qty_regressed" in mgr.anomalies()[0]


@pytest.mark.parametrize(
    "event,status,needle",
    [
        ("bogus", "new", "unknown_event"),
        ("new", "weird", "unknown_status"),
    ],
)
def test_unknown_event_or_status_is_an_anomaly(mgr, event, status, needle):
    assert mgr.apply(_update(event, status=status)) is None
    assert mgr.state("cid-1") is None
    assert len(mgr.anomalies()) == 1
    assert needle in mgr.anomalies()[0]


def test_rejection_reason_is_preserved(mgr):
    mgr.apply(_update("accepted"))
    ev = _update("rejected", status="rejected")
    ev["order"]["reject_reason"] = "insufficient buying power"
    state = mgr.apply(ev)
    assert state is not None
    assert state.reason == "insufficient buying power"


def test_open_orders_excludes_terminals_and_is_sorted(mgr):
    mgr.apply(_update("new", cid="b"))
    mgr.apply(_update("new", cid="a"))
    mgr.apply(_update("new", cid="c"))
    mgr.apply(_update("canceled", cid="b", status="canceled"))
    assert [s.client_order_id for s in mgr.open_orders()] == ["a", "c"]
    assert mgr.state("b").is_terminal


def test_restore_seeds_from_broker_states(mgr):
    broker = (
        OrderState("cid-2", "ord-2", "MSFT", "sell", 5, 5, 20.0, "filled", TS),
        OrderState("cid-1", "ord-1", "AAPL", "buy", 10, 4, 10.5, "partially_filled", TS),
    )
    mgr.restore(broker)
    seeded = mgr.state("cid-1")
    assert seeded is not None
    assert seeded.filled_qty == 4
    assert seeded.avg_fill_price == pytest.approx(10.5)
    assert mgr.state("cid-2").is_terminal
    assert [s.client_order_id for s in mgr.open_orders()] == ["cid-1"]
    # A legal continuation after restore advances from the broker's numbers:
    # (4*10.5 + 6*11.0) / 10 = 10.80.
    state = mgr.apply(
        _update("fill", status="filled", fill_qty="6", price="11.00", filled_qty="10")
    )
    assert state.status == "filled"
    assert state.filled_qty == 10
    assert state.avg_fill_price == pytest.approx(10.80)
    assert mgr.anomalies() == ()


def test_restore_rejects_an_inconsistent_state(mgr):
    broker = (OrderState("cid-1", "ord-1", "AAPL", "buy", 10, 11, None, "partially_filled", TS),)
    mgr.restore(broker)
    assert mgr.state("cid-1") is None
    assert len(mgr.anomalies()) == 1
    assert "restore_inconsistent_qty" in mgr.anomalies()[0]


def test_anomalies_records_each_with_the_raw_event(mgr):
    mgr.apply(_update("bogus", status="new"))
    mgr.apply(_update("new", cid="cid-2", status="weird"))
    mgr.apply(_update("new", cid="cid-3", qty="abc"))
    anomalies = mgr.anomalies()
    assert len(anomalies) == 3
    assert all("raw_event=" in a for a in anomalies)
    assert "cid-2" in anomalies[1]
    assert "cid-3" in anomalies[2]


def test_anomaly_is_audited_with_the_raw_event(cfg, audit):
    mgr = OrderStateManager(now=cfg.now, audit=audit)
    raw = _update("bogus", status="new")
    mgr.apply(raw)
    rows = audit.read()
    anomalies = [r for r in rows if r["kind"] == "order_anomaly"]
    assert len(anomalies) == 1
    assert anomalies[0]["reason"] == "unknown_event"
    assert anomalies[0]["data"]["raw_event"] == raw
    assert audit.verify()[0]


def test_applied_events_are_audited(cfg, audit):
    mgr = OrderStateManager(now=cfg.now, audit=audit)
    mgr.apply(_update("accepted"))
    mgr.apply(_update("fill", status="filled", fill_qty="10", price="12.00", filled_qty="10"))
    kinds = [r["kind"] for r in audit.read()]
    assert kinds == ["order_state", "order_fill", "order_state"]
    assert audit.verify()[0]


def test_missing_timestamp_falls_back_to_the_injected_clock(cfg, now):
    mgr = OrderStateManager(now=cfg.now)
    state = mgr.apply(_update("new", ts=None))
    assert state is not None
    assert state.updated_at == now.isoformat(timespec="seconds")
