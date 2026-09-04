"""Watch discovery tests: reports-tree mode, latest-per-symbol, inbox mode."""

from __future__ import annotations

import json
import time
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
