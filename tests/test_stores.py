"""Store tests: audit chain tamper detection, journal idempotency, signal feed."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from signald.stores import AuditChain, Journal, SignalStore

pytestmark = pytest.mark.timeout(120)

NOW = datetime(2026, 9, 3, 12, 0)


def test_audit_append_and_verify_clean(tmp_path):
    audit = AuditChain(tmp_path / "audit.jsonl", lambda: NOW)
    audit.append("accepted", "all gates passed", ticker="AVGO", signal_id="sg-1")
    audit.append("rejected", "cash below reserve", ticker="MSFT")
    ok, idx, detail = audit.verify()
    assert ok and idx == -1
    assert len(audit.read()) == 2


def test_audit_tamper_breaks_chain(tmp_path):
    audit = AuditChain(tmp_path / "audit.jsonl", lambda: NOW)
    for i in range(3):
        audit.append("test", f"row {i}")
    rows = audit.read()
    # tamper with the middle row's reason
    rows[1]["reason"] = "MUTATED"
    rows[1]["hash"] = "0" * 64
    path = tmp_path / "audit.jsonl"
    path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")
    ok, idx, _ = AuditChain(path, lambda: NOW).verify()
    assert not ok
    assert idx == 1  # first bad index reported


def test_audit_empty_ledger_verifies(tmp_path):
    ok, idx, _ = AuditChain(tmp_path / "audit.jsonl", lambda: NOW).verify()
    assert ok and idx == -1


def test_journal_idempotency(tmp_path):
    j = Journal(tmp_path / "journal.jsonl", lambda: NOW)
    assert not j.is_processed("h1")
    j.mark_processed("h1", "AVGO", "/x/avgo.json")
    assert j.is_processed("h1")
    assert not j.is_processed("h2")


def test_journal_signal_counts_and_cooldown(tmp_path):
    j = Journal(tmp_path / "journal.jsonl", lambda: NOW)
    env = {
        "signal_id": "sg-1", "decision_hash": "h1", "ticker": "AVGO", "action": "REDUCE",
        "emitted_at": NOW.isoformat(), "gates": {"target_notional_usd": 0.0},
    }
    j.add_signal(env)
    assert j.signals_today_count() == 1
    assert j.signals_today_count(NOW + timedelta(days=1)) == 0
    last = j.last_signal()
    assert last and last["ticker"] == "AVGO" and last["action"] == "REDUCE"


def test_signal_store_append_and_latest(tmp_path):
    store = SignalStore(
        tmp_path / "signals" / "signals.jsonl", tmp_path / "signals" / "latest.json"
    )
    e1 = {"signal_id": "sg-1", "ticker": "AVGO", "action": "REDUCE", "emitted_at": NOW.isoformat()}
    e2 = {"signal_id": "sg-2", "ticker": "MSFT", "action": "BUY", "emitted_at": NOW.isoformat()}
    store.append(e1)
    store.append(e2)
    rows = store.read_all()
    assert [r["ticker"] for r in rows] == ["AVGO", "MSFT"]
    store.write_latest(e2)
    latest = json.loads((tmp_path / "signals" / "latest.json").read_text(encoding="utf-8"))
    assert latest["MSFT"]["signal_id"] == "sg-2"
    # latest per ticker keeps both
    store.write_latest(e1)
    latest = json.loads((tmp_path / "signals" / "latest.json").read_text(encoding="utf-8"))
    assert set(latest) == {"AVGO", "MSFT"}
