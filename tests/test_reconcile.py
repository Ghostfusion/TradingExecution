"""Reconciliation tests: broker is the source of truth (plan §4.4 C21, design §13).

Agreement, every drift kind, dict-vs-object inputs, tolerance handling, ordering,
read-only inputs, and the fail-closed one-sided cash case.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import pytest

from signald.reconcile import ReconcileReport, reconcile

pytestmark = pytest.mark.timeout(120)


@dataclass(frozen=True)
class Fill:
    symbol: str
    qty: float


@dataclass(frozen=True)
class Pos:
    symbol: str
    qty: float


@dataclass(frozen=True)
class Ord:
    client_order_id: str
    status: str = "accepted"
    symbol: str | None = None


def _ok(**kwargs: Any) -> ReconcileReport:
    return reconcile(
        local_fills=kwargs.pop("local_fills", [Fill("AAPL", 10.0)]),
        broker_positions=kwargs.pop("broker_positions", [Pos("AAPL", 10.0)]),
        now=kwargs.pop("now"),
        **kwargs,
    )


def test_agreement_is_ok_with_no_drift(now):
    report = _ok(now=now)
    assert isinstance(report, ReconcileReport)
    assert report.ok is True
    assert report.halt is False
    assert report.drift == ()
    assert report.detail == "in sync"
    assert report.checked_at == now.isoformat()


def test_a_position_mismatch_is_drift_and_halts(now):
    report = _ok(local_fills=[Fill("AAPL", 10.0)], broker_positions=[Pos("AAPL", 7.0)], now=now)
    assert report.ok is False
    assert report.halt is True
    (drift,) = report.drift
    assert drift.kind == "position"
    assert drift.symbol == "AAPL"
    assert drift.local == 10.0
    assert drift.broker == 7.0


def test_signed_fills_net_to_the_position(now):
    # A +10 entry and a -4 partial exit net to +6, which matches the broker.
    report = _ok(
        local_fills=[Fill("AAPL", 10.0), Fill("AAPL", -4.0)],
        broker_positions=[Pos("AAPL", 6.0)],
        now=now,
    )
    assert report.ok is True


def test_a_broker_position_with_no_fills_is_drift(now):
    report = _ok(local_fills=[], broker_positions=[Pos("TSLA", 25.0)], now=now)
    (drift,) = report.drift
    assert drift.kind == "position"
    assert drift.symbol == "TSLA"
    assert drift.local == 0.0
    assert drift.broker == 25.0
    assert "0" in drift.detail
    assert report.halt is True


def test_a_local_fill_with_no_broker_position_is_drift(now):
    report = _ok(local_fills=[Fill("AAPL", 10.0)], broker_positions=[], now=now)
    (drift,) = report.drift
    assert drift.kind == "position"
    assert drift.local == 10.0
    assert drift.broker == 0.0


def test_an_unknown_broker_order_is_drift(now):
    report = _ok(
        broker_orders=[Ord("ord-1", "accepted")],
        local_open_orders=[],
        now=now,
    )
    (drift,) = report.drift
    assert drift.kind == "order"
    assert report.halt is True
    assert "ord-1" in drift.detail


def test_a_known_broker_order_is_not_drift(now):
    report = _ok(
        broker_orders=[Ord("ord-1", "accepted")],
        local_open_orders=[{"client_order_id": "ord-1"}],
        now=now,
    )
    assert report.ok is True


@pytest.mark.parametrize("status", ["filled", "canceled", "expired", "rejected", "replaced"])
def test_a_closed_broker_order_is_ignored(now, status):
    report = _ok(broker_orders=[Ord("ord-1", status)], local_open_orders=[], now=now)
    assert report.ok is True


def test_a_broker_order_without_a_client_order_id_is_drift(now):
    report = _ok(
        broker_orders=[{"id": "ord-1", "status": "accepted"}],
        local_open_orders=[],
        now=now,
    )
    (drift,) = report.drift
    assert drift.kind == "order"
    assert "client_order_id" in drift.detail


def test_a_cash_mismatch_beyond_tolerance_is_drift(now):
    report = _ok(cash_local=100_000.0, cash_broker=99_000.0, now=now)
    (drift,) = report.drift
    assert drift.kind == "cash"
    assert drift.local == 100_000.0
    assert drift.broker == 99_000.0
    assert report.halt is True


def test_cash_within_tolerance_is_ok(now):
    report = _ok(cash_local=100_000.0, cash_broker=100_000.5, cash_tolerance=1.0, now=now)
    assert report.ok is True


def test_cash_tolerance_boundary_is_inclusive(now):
    report = _ok(cash_local=100_000.0, cash_broker=100_001.0, cash_tolerance=1.0, now=now)
    assert report.ok is True


def test_cash_known_on_one_side_only_is_drift(now):
    report = _ok(cash_local=100_000.0, cash_broker=None, now=now)
    (drift,) = report.drift
    assert drift.kind == "cash"
    assert report.halt is True


def test_share_tolerance_is_honoured(now):
    strict = _ok(local_fills=[Fill("AAPL", 10.0)], broker_positions=[Pos("AAPL", 10.5)], now=now)
    tolerant = _ok(
        local_fills=[Fill("AAPL", 10.0)],
        broker_positions=[Pos("AAPL", 10.5)],
        share_tolerance=1.0,
        now=now,
    )
    assert strict.halt is True
    assert tolerant.ok is True


@pytest.mark.parametrize("as_dict", [False, True])
def test_dict_and_object_inputs_agree(now, as_dict):
    fills: list[Any] = [Fill("AAPL", 10.0), Fill("TSLA", -5.0)]
    positions: list[Any] = [Pos("AAPL", 10.0), Pos("TSLA", -5.0)]
    orders: list[Any] = [Ord("ord-1", "accepted"), Ord("ord-2", "filled")]
    known: list[Any] = [{"client_order_id": "ord-1"}]
    if as_dict:
        fills = [{"symbol": f.symbol, "qty": f.qty} for f in fills]
        positions = [{"symbol": p.symbol, "qty": p.qty} for p in positions]
        orders = [{"client_order_id": o.client_order_id, "status": o.status} for o in orders]
    report = _ok(
        local_fills=fills,
        broker_positions=positions,
        broker_orders=orders,
        local_open_orders=known,
        cash_local=50_000.0,
        cash_broker=50_000.0,
        now=now,
    )
    assert report.ok is True


def test_dict_and_object_inputs_produce_the_same_drift(now):
    obj = _ok(local_fills=[Fill("AAPL", 10.0)], broker_positions=[], now=now)
    dct = _ok(local_fills=[{"symbol": "AAPL", "qty": 10.0}], broker_positions=[], now=now)
    assert obj.drift == dct.drift


def test_drift_is_deterministically_ordered(now):
    report = _ok(
        local_fills=[Fill("TSLA", 1.0), Fill("AAPL", 1.0)],
        broker_positions=[
            Pos("TSLA", 2.0),
            Pos("AAPL", 2.0),
            Pos("MSFT", 3.0),
        ],
        broker_orders=[Ord("ord-1", "accepted")],
        local_open_orders=[],
        cash_local=1.0,
        cash_broker=5.0,
        now=now,
    )
    assert [d.kind for d in report.drift] == ["cash", "order", "position", "position", "position"]
    assert [d.symbol for d in report.drift][2:] == ["AAPL", "MSFT", "TSLA"]


def test_inputs_are_never_mutated(now):
    fills = [{"symbol": "AAPL", "qty": 10.0}]
    positions = [Pos("AAPL", 10.0)]
    orders = [{"client_order_id": "ord-1", "status": "accepted"}]
    known = [{"client_order_id": "ord-1"}]
    snapshots = copy.deepcopy((fills, positions, orders, known))
    reconcile(
        local_fills=fills,
        broker_positions=positions,
        broker_orders=orders,
        local_open_orders=known,
        cash_local=1.0,
        cash_broker=1.0,
        now=now,
    )
    assert (fills, positions, orders, known) == snapshots


def test_a_malformed_fill_is_rejected_fail_closed(now):
    with pytest.raises(ValueError):
        _ok(local_fills=[{"symbol": "AAPL"}], broker_positions=[], now=now)


def test_a_malformed_broker_position_is_rejected_fail_closed(now):
    with pytest.raises(ValueError):
        _ok(local_fills=[], broker_positions=[{"qty": 3.0}], now=now)


def test_an_ok_report_has_no_drift_and_no_halt(now):
    report = _ok(cash_local=1.0, cash_broker=1.0, now=now)
    assert report.ok is True
    assert report.halt is False
    assert report.drift == ()
    assert isinstance(report.drift, tuple)
