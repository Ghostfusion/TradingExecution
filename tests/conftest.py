"""Shared hermetic fixtures: fake clock, injectable transport seam, tmp dirs.

The Alpaca reference and webhook dispatcher are replaced by the seam — the
full pipeline runs with ZERO network access.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time
from pathlib import Path

import pytest

from signald.alpaca_ref import AlpacaReference
from signald.config import _PREFIXES, Config
from signald.mandate import DEFAULT_MANDATE, load_mandate, write_mandate
from signald.notifier import Notifier
from signald.processor import SignalProcessor
from signald.stores import AuditChain, Journal, SignalStore

# The injected clock is "today at noon": `build_sample()` stamps `date.today()`
# as the effective date, so a fixed past date here made every processor test
# fail the no-lookahead precheck as soon as the wall clock moved past it. The
# date is derived once at import - no test reads the clock by itself.
NOW = datetime.combine(date.today(), time(12, 0))

pytestmark = pytest.mark.timeout(120)


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch):
    """No test may inherit this developer's shell (load_config prefers it to .env).

    A real ``TRADINGEXEC_*``/``ALPACA_*`` export - the daemon's own env when it
    runs from this shell - silently outranks the test's ``--env`` file and
    redirects the watch dir, the mandate or the webhook. Strip them for every
    test; a test that wants one sets it itself.
    """
    for key in [k for k in os.environ if k.startswith(_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def transport_state():
    return {
        "quote": {
            "last": 356.99, "vwap": 350.04, "spread_usd": 0.12,
            "ts": NOW.isoformat(), "feed": "iex",
        },
        "account": {"cash": 80000.0, "buying_power": 100000.0, "equity": 110000.0},
        "clock": {"is_open": True},
        "asset": {"tradable": True},
        "positions": {"positions_value": 10000.0},
    }


@pytest.fixture
def seam(transport_state):
    def transport(method, ticker):
        if method not in transport_state:
            return None
        return dict(transport_state[method])

    transport.state = transport_state
    return transport


@pytest.fixture
def webhook_events():
    return []


@pytest.fixture
def notifier(webhook_events):
    def sink(event):
        webhook_events.append(event)

    return Notifier(transport=sink, now=lambda: NOW)


@pytest.fixture
def cfg(tmp_path, now):
    c = Config(
        watch_dir=tmp_path / "decisions",
        data_dir=tmp_path / "signals",
        audit_file=tmp_path / "audit" / "audit.jsonl",
        journal_file=tmp_path / "audit" / "journal.jsonl",
        mandate_path=tmp_path / "mandate.json",
        kill_switch_path=tmp_path / "kill_switch",
        halt_latch_path=tmp_path / "audit" / "halt_episode.json",
        heartbeat_path=tmp_path / "audit" / "heartbeat",
        pid_file=tmp_path / "signald.pid",
        now_fn=lambda: now,
    )
    return c


@pytest.fixture
def mandate(cfg):
    write_mandate(cfg.mandate_path, DEFAULT_MANDATE)
    return load_mandate(cfg.mandate_path)


@pytest.fixture
def processor(cfg, mandate, seam, notifier):
    audit = AuditChain(cfg.audit_file, cfg.now)
    journal = Journal(cfg.journal_file, cfg.now)
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    ref = AlpacaReference(transport=seam)
    return SignalProcessor(cfg, mandate, store, journal, audit, ref, notifier)


@pytest.fixture
def write_artifact(cfg):
    def _write(doc: dict) -> Path:
        cfg.watch_dir.mkdir(parents=True, exist_ok=True)
        p = cfg.watch_dir / f"{doc['ticker']}_decision.json"
        import json
        p.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        return p

    return _write
