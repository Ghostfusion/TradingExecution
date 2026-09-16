"""Watch discovery tests: reports-tree mode, latest-per-symbol, inbox mode."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from signald.config import Config
from signald.samples import build_sample
from signald.watch import ARTIFACT_NAME, WatchLoop

pytestmark = pytest.mark.timeout(120)

# fix clock import to match conftest usage
from signald.alpaca_ref import AlpacaReference  # noqa: E402
from signald.notifier import Notifier  # noqa: E402
from signald.processor import SignalProcessor  # noqa: E402
from signald.stores import AuditChain, Journal, SignalStore  # noqa: E402


def _proc(cfg, seam, mandate=None):
    from signald.kill_switch import is_halted  # noqa: F401

    if mandate is None:
        from signald.mandate import DEFAULT_MANDATE, load_mandate, write_mandate

        write_mandate(cfg.mandate_path, DEFAULT_MANDATE)
        mandate = load_mandate(cfg.mandate_path)
    audit = AuditChain(cfg.audit_file, cfg.now)
    journal = Journal(cfg.journal_file, cfg.now)
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    ref = AlpacaReference(transport=seam)
    return SignalProcessor(cfg, mandate, store, journal, audit, ref, Notifier(now=cfg.now))


def _write_decision(folder, ticker: str, mtime: float):
    folder.mkdir(parents=True, exist_ok=True)
    p = Path(str(folder)) / ARTIFACT_NAME
    p.write_text(json.dumps(build_sample(ticker=ticker)), encoding="utf-8")
    os_utime(p, mtime)
    return p


def os_utime(p, mtime):
    import os

    os.utime(p, (mtime, mtime))


def test_reports_mode_latest_per_symbol(cfg, seam):
    cfg = Config(**{**cfg.__dict__, "watch_recursive": True, "latest_only": True})
    proc = _proc(cfg, seam)
    loop = WatchLoop(proc)
    base = cfg.watch_dir
    t0 = time.time()
    # AVGO: older + newer folders; MSFT: single; noise run_card.json
    _write_decision(base / "AVGO_20260902_100000", "AVGO", t0 - 5000)
    newer = _write_decision(base / "AVGO_20260903_100000", "AVGO", t0)
    _write_decision(base / "MSFT_20260903_090000", "MSFT", t0)
    (base / "AVGO_20260902_100000" / "run_card.json").write_text("{}", encoding="utf-8")

    found = loop.discover()
    # only newer AVGO + MSFT decision files; run_card ignored; AVGO older excluded
    assert len(found) == 2
    assert any(str(p) == str(newer) for p in found)
    assert all(p.name == ARTIFACT_NAME for p in found)


def test_reports_mode_all_when_latest_off(cfg, seam):
    cfg = Config(**{**cfg.__dict__, "watch_recursive": True, "latest_only": False})
    proc = _proc(cfg, seam)
    loop = WatchLoop(proc)
    t0 = time.time()
    _write_decision(cfg.watch_dir / "AVGO_20260902_100000", "AVGO", t0 - 100)
    _write_decision(cfg.watch_dir / "AVGO_20260903_100000", "AVGO", t0)
    assert len(loop.discover()) == 2


def test_inbox_mode_flat(cfg, seam):
    cfg = Config(**{**cfg.__dict__, "watch_recursive": False, "latest_only": True})
    proc = _proc(cfg, seam)
    loop = WatchLoop(proc)
    (cfg.watch_dir).mkdir(parents=True, exist_ok=True)
    p1 = cfg.watch_dir / "research_decision.json"
    p1.write_text(json.dumps(build_sample(ticker="AVGO")), encoding="utf-8")
    found = loop.discover()
    assert len(found) == 1 and found[0] == p1


def test_discover_empty_when_no_dir(cfg, seam):
    proc = _proc(cfg, seam)
    loop = WatchLoop(proc)
    assert loop.discover() == []


# --- the scan window (2026-09-15: scan only while the regular session is open) ---
OPEN = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)                      # Tue 10:00 ET
PRE_OPEN = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)                  # Tue 08:00 ET
AFTER_CLOSE = datetime(2026, 9, 15, 21, 0, tzinfo=UTC)               # Tue 17:00 ET
WEEKEND = datetime(2026, 9, 19, 14, 0, tzinfo=UTC)                   # Sat 10:00 ET
HOLIDAY = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)                  # Thanksgiving
HALF_DAY_OPEN = datetime(2026, 11, 27, 16, 30, tzinfo=UTC)          # 11:30 ET (13:00 close)
HALF_DAY_CLOSED = datetime(2026, 11, 27, 18, 30, tzinfo=UTC)        # 13:30 ET


def _at(cfg, stamp, **overrides):
    """The same config, pinned to an *aware* stamp (the window needs no host tz)."""
    return Config(**{**cfg.__dict__, "now_fn": (lambda s=stamp: s), **overrides})


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [(OPEN, True), (PRE_OPEN, False), (AFTER_CLOSE, False), (WEEKEND, False),
     (HOLIDAY, False), (HALF_DAY_OPEN, True), (HALF_DAY_CLOSED, False)],
    ids=["rth", "pre", "post", "weekend", "holiday", "half-day-open", "half-day-closed"],
)
def test_the_scan_window_follows_the_regular_session(cfg, seam, stamp, expected):
    """An ET fact (09:30-16:00, 13:00 half days) - the host clock's zone is irrelevant."""
    assert WatchLoop(_proc(_at(cfg, stamp), seam)).scan_window().open is expected


def test_the_closed_window_says_why(cfg, seam):
    window = WatchLoop(_proc(_at(cfg, WEEKEND), seam)).scan_window()
    assert window.reason == "closed" and "not scanning" in window.detail


def test_an_unknown_broker_clock_does_not_scan(cfg, seam, transport_state):
    """Fail closed: a calendar-open market with no broker confirmation waits."""
    transport_state.pop("clock", None)
    window = WatchLoop(_proc(_at(cfg, OPEN), seam)).scan_window()
    assert not window.open and window.reason == "broker_unknown"


def test_a_broker_closed_session_does_not_scan(cfg, seam, transport_state):
    """The broker knows about halts and early closes the shipped calendar cannot."""
    transport_state["clock"] = {"is_open": False}
    window = WatchLoop(_proc(_at(cfg, OPEN), seam)).scan_window()
    assert not window.open and window.reason == "broker_closed"


def test_the_window_can_be_disabled_or_trusted_without_the_broker(cfg, seam, transport_state):
    transport_state["clock"] = {"is_open": False}
    assert WatchLoop(_proc(_at(cfg, AFTER_CLOSE, scan_rth_only=False), seam)).scan_window().open
    trusted = WatchLoop(_proc(_at(cfg, OPEN, scan_confirm_broker_clock=False), seam)).scan_window()
    assert trusted.open and trusted.reason == "rth"


def _drive(loop, iterations):
    """Run the loop for ``iterations`` cycles, counting the scans it performs."""
    scans = []
    loop.run_once = lambda: scans.append(1) or []
    seen = {"n": 0}

    def stop():
        seen["n"] += 1
        return seen["n"] > iterations

    loop.run_forever(stop=stop)
    return scans


def test_run_forever_does_not_scan_outside_the_session(cfg, seam):
    cfg = _at(cfg, AFTER_CLOSE)
    assert _drive(WatchLoop(_proc(cfg, seam), poll_seconds=0.01), 3) == []
    # Idle, not dead: the watchdog must still find a fresh heartbeat.
    assert Path(cfg.heartbeat_path).exists()


def test_run_forever_scans_inside_the_session(cfg, seam):
    assert len(_drive(WatchLoop(_proc(_at(cfg, OPEN), seam), poll_seconds=0.01), 2)) == 2


def test_run_once_ignores_the_window_for_the_operator(cfg, seam):
    """`signald run --once` is the override: it scans whenever the operator says."""
    cfg = _at(cfg, AFTER_CLOSE)
    _write_decision(cfg.watch_dir, "AVGO", time.time())
    assert [r.kind for r in WatchLoop(_proc(cfg, seam)).run_once()] == ["emitted"]
