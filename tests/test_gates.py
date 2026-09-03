"""Gate unit tests — every gate fail-closed (no network, seam fixture)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from signald.alpaca_ref import RefData
from signald.gates import evaluate
from signald.samples import build_sample
from signald.schema import build_signal_contract, parse_research_decision, sha256_of

pytestmark = pytest.mark.timeout(120)

NOW = datetime(2026, 9, 3, 12, 0)


def _doc(**kw):
    d = build_sample(**kw)
    d["effective_date"] = "2026-09-03"
    if "invalidations" not in kw:
        d["invalidations"] = []
    return _rehash(d)


def _rehash(doc):
    """Recompute decision_hash after a test mutates the artifact."""
    body = {k: v for k, v in doc.items() if k != "decision_hash"}
    doc["decision_hash"] = "sha256:" + sha256_of(body)
    return doc


def _components(mandate, doc=None, ref=None, journal_state=None):
    doc = doc or _doc()
    rd = parse_research_decision(doc)
    contract = build_signal_contract(
        rd, mandate.expires.isoformat(), NOW, ref.equity if ref else None
    )
    state = {
        "signals_today": 0,
        "last_signal": None,
        "cooldown_hours": 12.0,
        "ingest_window_hours": 24.0,
        "approval_threshold_factor": 1.0,
        **(journal_state or {}),
    }
    return rd, contract, state


def _ref(**kw):
    base = {
        "last": 356.99, "vwap": 350.04, "spread_usd": 0.12, "ts": NOW, "feed": "iex",
        "cash": 80000.0, "buying_power": 100000.0, "equity": 110000.0,
        "market_open": True, "asset_tradable": True, "positions_value": 10000.0,
    }
    base.update(kw)
    return RefData(**base)


def test_all_pass(mandate):
    rd, contract, state = _components(mandate)
    g = evaluate(rd, contract, mandate, _ref(), state, NOW)
    assert g.verdict == "PASS"
    assert g.passed and not g.approval_required


def test_symbol_not_allowed_blocks(mandate):
    rd, contract, state = _components(mandate, _doc(ticker="ZZZZ"))
    g = evaluate(rd, contract, mandate, _ref(), state, NOW)
    assert g.verdict == "BLOCK"
    assert any("not in mandate" in r for r in g.blocked)


def test_cash_below_reserve_blocks(mandate):
    rd, contract, state = _components(mandate)
    g = evaluate(rd, contract, mandate, _ref(cash=1000.0), state, NOW)
    assert g.verdict == "BLOCK"
    assert any("cash" in r.lower() for r in g.blocked)


def test_ref_incomplete_blocks(mandate):
    rd, contract, state = _components(mandate)
    g = evaluate(rd, contract, mandate, _ref(cash=None, asset_tradable=True), state, NOW)
    assert g.verdict == "BLOCK"
    assert any("reference" in r.lower() for r in g.blocked)


def test_untradable_blocks(mandate):
    rd, contract, state = _components(mandate)
    g = evaluate(rd, contract, mandate, _ref(asset_tradable=False), state, NOW)
    assert g.verdict == "BLOCK"
    assert any("not tradable" in r for r in g.blocked)


def test_market_closed_downgrades(mandate):
    rd, contract, state = _components(mandate)
    g = evaluate(rd, contract, mandate, _ref(market_open=False), state, NOW)
    assert g.verdict == "DOWNGRADE"
    assert any("next-session" in r for r in g.downgrades)


def test_stale_data_quality_blocks(mandate):
    rd, contract, state = _components(mandate, _doc(data_quality="stale"))
    g = evaluate(rd, contract, mandate, _ref(), state, NOW)
    assert g.verdict == "BLOCK"


def test_partial_data_downgrades(mandate):
    rd, contract, state = _components(mandate, _doc(data_quality="partial"))
    g = evaluate(rd, contract, mandate, _ref(), state, NOW)
    assert g.verdict == "DOWNGRADE"


def test_stale_ref_downgrades(mandate):
    rd, contract, state = _components(mandate)
    old = NOW - timedelta(minutes=30)
    g = evaluate(rd, contract, mandate, _ref(ts=old, stale=True), state, NOW)
    assert g.verdict == "DOWNGRADE"
    assert any("stale" in r for r in g.downgrades)


def test_size_over_cap_downgrades(mandate):
    doc = _doc(allocation_pct=1.0)
    doc["position"]["target_notional"] = 50000.0  # > 20000 cap
    rd, contract, state = _components(mandate, _rehash(doc))
    g = evaluate(rd, contract, mandate, _ref(), state, NOW)
    assert g.verdict == "DOWNGRADE"
    assert any("cap" in r for r in g.downgrades)


def test_exposure_over_cap_downgrades(mandate):
    doc = _doc(allocation_pct=1.0)
    doc["position"]["target_notional"] = 150000.0
    rd, contract, state = _components(mandate, _rehash(doc))
    g = evaluate(rd, contract, mandate, _ref(positions_value=100000.0), state, NOW)
    assert g.verdict == "DOWNGRADE"
    assert any("exposure" in r for r in g.downgrades)


def test_daily_cap_blocks(mandate):
    rd, contract, state = _components(mandate, journal_state={"signals_today": 12})
    g = evaluate(rd, contract, mandate, _ref(), state, NOW)
    assert g.verdict == "BLOCK"
    assert any("daily" in r for r in g.blocked)


def test_cooldown_downgrades(mandate):
    last = {"ticker": "AVGO", "action": "REDUCE",
            "emitted_at": (NOW - timedelta(hours=2)).isoformat()}
    rd, contract, state = _components(mandate, journal_state={"last_signal": last})
    g = evaluate(rd, contract, mandate, _ref(), state, NOW)
    assert g.verdict == "DOWNGRADE"
    assert any("cooldown" in r for r in g.downgrades)


def test_same_line_no_cooldown(mandate):
    rd, contract, state = _components(mandate, journal_state={
        "last_signal": {"ticker": "AVGO", "action": "HOLD",
                        "emitted_at": (NOW - timedelta(hours=2)).isoformat()}})
    g = evaluate(rd, contract, mandate, _ref(), state, NOW)
    assert g.blocked == ()


def test_invalidation_breach_blocks(mandate):
    doc = _doc()
    doc["invalidations"] = ["price_stop_loss: breach below 429.0"]
    rd, contract, state = _components(mandate, _rehash(doc))
    g = evaluate(rd, contract, mandate, _ref(last=500.0), state, NOW)  # above stop 429 -> no breach
    assert g.blocked == ()
    g2 = evaluate(rd, contract, mandate, _ref(last=420.0), state, NOW)  # 420 <= 429 -> breach
    assert g2.verdict == "BLOCK"
    assert any("invalidation" in r for r in g2.blocked)


def test_old_decision_blocks(mandate):
    doc = _doc()
    doc["effective_date"] = (date.today() - timedelta(days=3)).isoformat()
    rd, contract, state = _components(mandate, _rehash(doc))
    g = evaluate(rd, contract, mandate, _ref(), state, NOW)
    assert g.verdict == "BLOCK"
    assert any("ingest" in r for r in g.blocked)


def test_approval_required_on_large_notional(mandate):
    doc = _doc(allocation_pct=1.0)
    doc["position"]["target_notional"] = 25000.0  # >= 20000 threshold
    rd, contract, state = _components(mandate, _rehash(doc))
    g = evaluate(rd, contract, mandate, _ref(), state, NOW)
    assert g.approval_required is True
    assert g.verdict == "DOWNGRADE"  # approval tag surfaces as downgrade, not block
