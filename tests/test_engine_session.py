"""The paper-session drill: scan -> gate -> size -> guard -> submit -> flat -> reconcile.

This is P3's exit criterion ("one full paper session") driven through the real
composition with injected market-data and order seams. The two structural
promises are asserted here as well: no order path in `signal` mode, and none
without an explicit `execute=True`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from signald.config import load_config
from signald.engine import SessionEngine
from signald.mandate import DEFAULT_MANDATE, load_mandate, write_mandate
from signald.marketdata.service import MarketDataService
from signald.order.manager import OrderManager
from signald.risk.state import BookState, Position

pytestmark = pytest.mark.timeout(120)

SESSION = datetime(2026, 9, 12, 9, 45)


# --- fake venue seams ------------------------------------------------------
def _bar(ts: datetime, open_: float, close: float, volume: float) -> dict:
    return {
        "ts": ts.isoformat(),
        "open": open_,
        "high": max(open_, close) + 0.05,
        "low": min(open_, close) - 0.05,
        "close": close,
        "volume": volume,
    }


def intraday_bars_until(
    cutoff: datetime, *, start: float = 100.0, step: float = 0.08
) -> list[dict]:
    """Every 1-minute bar from 09:30 up to ``cutoff`` (a rising trend day).

    A venue returns the most recent bars, so the series must *end* at the
    decision time - a series that starts there would look hours stale.
    """
    origin = cutoff.replace(hour=9, minute=30, second=0, microsecond=0)
    count = max(1, int((cutoff - origin).total_seconds() // 60) + 1)
    out, price = [], start
    for i in range(count):
        nxt = price + step
        out.append(_bar(origin + timedelta(minutes=i), price, nxt, 60_000.0))
        price = nxt
    return out


def daily_bars(count: int, *, close: float = 100.0, volume: float = 2_000_000.0) -> list[dict]:
    """Flat daily closes with a $1 range: ATR ~1.0 (> min_atr), ADV 2M (> min_adv)."""
    origin = SESSION.date() - timedelta(days=count)
    out = []
    for i in range(count):
        day = datetime.combine(origin + timedelta(days=i), datetime.min.time())
        out.append(
            {
                "ts": day.isoformat(),
                "open": close,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": volume,
            }
        )
    return out


class FakeVenue:
    """A market-data transport that answers quote/bars/daily from generated series."""

    def __init__(self, *, quote_ts: datetime | None = None, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.quote_ts = quote_ts

    def __call__(self, method: str, payload: dict):
        self.calls.append((method, payload))
        if self.fail:
            return None
        if method == "quote":
            last = intraday_bars_until(self.quote_ts or SESSION)[-1]["close"]
            return {
                "bid": last - 0.01,
                "ask": last + 0.01,
                "last": last,
                "as_of": (self.quote_ts or SESSION).isoformat(),
                "feed": "sip",
            }
        if method == "bars":
            count = int(payload["count"])
            if payload.get("timeframe", "1Min") == "1Min":
                cutoff = datetime.fromisoformat(payload["as_of"])
                full = intraday_bars_until(cutoff)
                return {"feed": "sip", "bars": full[-count:]}
            return {"feed": "sip", "bars": daily_bars(count)}
        return None


class FakeBroker:
    """An order transport that accepts everything and reports it back."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, payload: dict):
        self.calls.append((method, payload))
        if method == "submit":
            return {
                "id": "ord-1",
                "client_order_id": payload.get("client_order_id"),
                "status": "accepted",
                "qty": str(payload.get("qty", 0)),
                "filled_qty": "0",
            }
        if method == "query":
            return {"id": "ord-1", "status": "accepted"}
        return {"status": "canceled"}

    def submitted(self) -> list[dict]:
        return [p for m, p in self.calls if m == "submit"]


# --- fixtures --------------------------------------------------------------
@pytest.fixture()
def paper_cfg(tmp_path):
    """Paper mode, intraday armed, and a calibration the shipped defaults cannot reach.

    Two documented default mismatches are relaxed here so the *flow* is what the
    test exercises (both are reported in the CHANGELOG):
    * the 1% house ES budget is below the stress-grid floor (a 25%-of-sleeve
      cluster gapping 10% is already 0.75% before any volatility term), and
    * 0.25% risk at a 1.75xATR stop implies a ~14% position, above the 5%
      single-name and 25%-of-sleeve cluster caps, so every trade would be
      blocked, not just reduced.
    """
    cfg = load_config(env_file=tmp_path / "absent.env", environ={})
    return replace(
        cfg,
        mode="paper",
        intraday_enabled=True,
        es_budget_house_pct=0.02,
        es_budget_sleeve_pct=0.02,
        risk_per_trade_intraday_pct=0.001,
        now_fn=lambda: SESSION,
    )


@pytest.fixture()
def paper_mandate(paper_cfg):
    raw = dict(DEFAULT_MANDATE)
    raw["created"] = "2026-09-01"
    raw["expires"] = "2027-09-01"
    raw["max_notional_per_order_usd"] = 50_000
    raw["max_total_exposure_usd"] = 500_000
    write_mandate(paper_cfg.mandate_path, raw)
    return load_mandate(paper_cfg.mandate_path)


def engine(paper_cfg, paper_mandate, venue, broker, **kw) -> SessionEngine:
    audit = kw.pop("audit", None)
    returns = kw.pop("returns", tuple([0.0005, -0.0004] * 300))
    equity_history = kw.pop("equity_history", (100_000.0,))
    book = kw.pop("book", BookState(equity=100_000.0, cash=50_000.0, buying_power=200_000.0))
    broker_positions = kw.pop("broker_positions", None)
    manager = OrderManager(
        paper_cfg.data_dir / "orders_pending.jsonl", broker, lambda: SESSION, audit
    )
    return SessionEngine(
        config=paper_cfg,
        mandate=paper_mandate,
        marketdata=MarketDataService(venue, lambda: SESSION, paper_cfg),
        order_manager=manager,
        state_manager=kw.pop("state_manager", None),
        audit=audit,
        universe=("AVGO",),
        equity_history=equity_history,
        daily_returns=returns,
        book=book,
        broker_positions=broker_positions,
        **kw,
    )


# --- the session -----------------------------------------------------------
def test_a_paper_session_scans_gates_sizes_and_submits(paper_cfg, paper_mandate):
    broker = FakeBroker()
    eng = engine(paper_cfg, paper_mandate, FakeVenue(), broker)

    result = eng.run(execute=True)

    assert result.scanned == 1
    assert result.candidates and result.candidates[0]["setup"]
    submitted = broker.submitted()
    assert len(submitted) == 1, result.skipped
    assert submitted[0]["qty"] >= 1
    # the broker payload (manager-owned) never carries a market order
    assert submitted[0]["type"] in {"limit", "marketable_limit"}
    assert result.trades_today.get("AVGO") == 1
    assert result.flat is True
    assert result.submitted_count == 1


def test_without_execute_nothing_is_submitted(paper_cfg, paper_mandate):
    broker = FakeBroker()
    eng = engine(paper_cfg, paper_mandate, FakeVenue(), broker)

    result = eng.run(execute=False)

    assert result.candidates, "the scan still runs"
    assert broker.calls == [] and result.submitted_count == 0


def test_signal_mode_cannot_reach_the_order_path(paper_cfg, paper_mandate):
    broker = FakeBroker()
    signal_only = replace(paper_cfg, mode="signal")
    eng = engine(signal_only, paper_mandate, FakeVenue(), broker)

    result = eng.run(execute=True)

    assert broker.calls == []
    assert result.halted is True
    assert eng.armed(execute=True) == (False, "mode=signal: the order path is unreachable")


def test_the_time_gate_blocks_outside_the_entry_window(paper_cfg, paper_mandate):
    broker = FakeBroker()
    late_stamp = SESSION.replace(hour=12, minute=30)
    late = replace(paper_cfg, now_fn=lambda: late_stamp)
    eng = engine(late, paper_mandate, FakeVenue(quote_ts=late_stamp), broker)

    result = eng.run(execute=True)

    assert broker.submitted() == []
    gated = {g["binding_gate"] for g in result.gated if not g["allowed"]}
    assert "time" in gated, result.gated


def test_a_halt_rung_stops_the_session(paper_cfg, paper_mandate):
    broker = FakeBroker()
    eng = engine(
        paper_cfg,
        paper_mandate,
        FakeVenue(),
        broker,
        equity_history=(200_000.0, 100_000.0),  # -50% peak-to-trough -> rung 4
    )

    result = eng.run(execute=True)

    assert broker.submitted() == []
    assert result.halted is True
    assert any(g["binding_gate"] == "house_drawdown" for g in result.gated)


def test_a_quarantined_symbol_is_skipped_before_any_read(paper_cfg, paper_mandate):
    venue = FakeVenue()
    broker = FakeBroker()
    eng = engine(paper_cfg, paper_mandate, venue, broker)
    eng.md.quarantine("AVGO", "unit test")

    result = eng.run(execute=True)

    assert result.skip_reasons() == ("quarantined",)
    assert broker.calls == []


def test_an_unavailable_feed_quarantines_the_symbol(paper_cfg, paper_mandate):
    broker = FakeBroker()
    eng = engine(paper_cfg, paper_mandate, FakeVenue(fail=True), broker)

    result = eng.run(execute=True)

    assert result.skip_reasons() == ("data_unavailable",)
    assert eng.md.is_quarantined("AVGO")
    assert broker.calls == []


def test_a_stale_quote_never_produces_an_order(paper_cfg, paper_mandate):
    broker = FakeBroker()
    stale = SESSION - timedelta(minutes=5)  # beyond max_quote_staleness_s
    eng = engine(paper_cfg, paper_mandate, FakeVenue(quote_ts=stale), broker)

    result = eng.run(execute=True)

    assert broker.submitted() == []
    assert result.skip_reasons() == ("data_unavailable",)


def test_the_engine_uses_the_single_sizer_and_gate(paper_cfg, paper_mandate):
    """No second risk implementation: a poisoned gate stops the submission."""
    broker = FakeBroker()
    seen: list[object] = []

    def poisoned(ctx):
        seen.append(ctx)
        from signald.risk.gate import GateDecision

        return GateDecision(
            verdict="BLOCK",
            binding_gate="house_cvar",
            reasons=("BLOCK house_cvar: test",),
            state_snapshot={},
            decided_at=SESSION.isoformat(),
            config_hash=paper_cfg.config_hash(),
            permission_reason_code="es_unavailable",
        )

    eng = engine(paper_cfg, paper_mandate, FakeVenue(), broker, gate_evaluator=poisoned)

    result = eng.run(execute=True)

    assert seen and broker.submitted() == []
    assert result.gated[0]["reason"] == "es_unavailable"


def test_flatten_reports_no_position_when_the_book_is_flat(paper_cfg, paper_mandate):
    broker = FakeBroker()
    eng = engine(paper_cfg, paper_mandate, FakeVenue(), broker)

    result = eng.run(execute=True, flatten=False)

    assert result.flat is None and result.flat_detail == ""


def test_reconcile_is_skipped_without_a_state_manager(paper_cfg, paper_mandate):
    """No fill stream means no reconciliation claim (never a silent 'ok')."""
    broker = FakeBroker()
    eng = engine(paper_cfg, paper_mandate, FakeVenue(), broker)

    result = eng.run(execute=True)

    assert result.reconciled is None and result.drift == ()

    # and with a state manager whose broker agrees, it reports ok
    from signald.order.state import OrderStateManager

    states = OrderStateManager(lambda: SESSION)
    positions: list = []
    broker2 = FakeBroker()
    eng2 = engine(
        paper_cfg,
        paper_mandate,
        FakeVenue(),
        broker2,
        state_manager=states,
        broker_positions=lambda: positions,
    )
    result2 = eng2.run(execute=True)

    assert result2.reconciled is True and result2.drift == ()


def test_the_position_book_is_never_mutated(paper_cfg, paper_mandate):
    broker = FakeBroker()
    book = BookState(
        equity=100_000.0,
        cash=50_000.0,
        positions=(Position(symbol="MSFT", qty=10, avg_entry=100.0, last=100.0),),
    )
    eng = engine(paper_cfg, paper_mandate, FakeVenue(), broker, book=book)

    eng.run(execute=True)

    assert book.positions == (Position(symbol="MSFT", qty=10, avg_entry=100.0, last=100.0),)
