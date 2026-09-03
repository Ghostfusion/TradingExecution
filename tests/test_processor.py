"""End-to-end processor tests: pipeline, idempotency, kill switch, notifier."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from signald.kill_switch import is_halted
from signald.samples import build_sample
from signald.stores import AuditChain, Journal, SignalStore

pytestmark = pytest.mark.timeout(120)


def test_happy_path_emits_signal(processor, write_artifact, cfg, seam, webhook_events):
    p = write_artifact(build_sample())
    res = processor.process(p)
    assert res.kind == "emitted", res.reasons
    env = res.envelope
    assert env["ticker"] == "AVGO" and env["action"] == "REDUCE"
    assert env["ref"]["last"] == 356.99
    band = env["expected_cost_band_bps"]
    assert len(band) == 2 and 0 < band[0] <= band[1]
    assert env["gates"]["verdict"] in {"PASS", "DOWNGRADE"}
    # persisted
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    assert len(store.read_all()) == 1
    # notified
    assert len(webhook_events) == 1 and webhook_events[0]["event"] == "signal"
    # audited
    audit = AuditChain(cfg.audit_file, cfg.now)
    assert any(r["kind"] == "accepted" for r in audit.read())


def test_duplicate_artifact_skipped(processor, write_artifact, cfg):
    p = write_artifact(build_sample())
    assert processor.process(p).kind == "emitted"
    res = processor.process(p)
    assert res.kind == "skipped_duplicate"
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    assert len(store.read_all()) == 1


def test_restart_recovery_no_duplicate(processor, cfg, write_artifact):
    """Kill and rebuild the daemon: journal replay prevents double emission."""
    p = write_artifact(build_sample())
    assert processor.process(p).kind == "emitted"
    # fresh processor over the SAME stores (simulated restart)
    from signald.alpaca_ref import AlpacaReference
    from signald.notifier import Notifier
    from signald.processor import SignalProcessor

    audit2 = AuditChain(cfg.audit_file, cfg.now)
    journal2 = Journal(cfg.journal_file, cfg.now)
    store2 = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    ref2 = AlpacaReference(transport=processor.ref._transport)
    proc2 = SignalProcessor(cfg, processor.mandate, store2, journal2, audit2, ref2,
                            Notifier(transport=lambda e: None, now=cfg.now))
    res = proc2.process(p)
    assert res.kind == "skipped_duplicate"
    assert len(store2.read_all()) == 1


def test_dry_run_persists_nothing(processor, cfg, write_artifact, seam):
    from signald.config import Config

    dry = Config(**{**cfg.__dict__, "dry_run": True})
    from signald.alpaca_ref import AlpacaReference
    from signald.notifier import Notifier
    from signald.processor import SignalProcessor

    audit = AuditChain(dry.audit_file, dry.now)
    journal = Journal(dry.journal_file, dry.now)
    store = SignalStore(dry.data_dir / "signals.jsonl", dry.data_dir / "latest.json")
    proc = SignalProcessor(dry, processor.mandate, store, journal, audit,
                           AlpacaReference(transport=seam), Notifier(now=dry.now))
    res = proc.process(write_artifact(build_sample()))
    assert res.kind == "dry_run"
    assert len(store.read_all()) == 0
    assert not journal.is_processed(res.envelope["decision_hash"])
    assert not (cfg.data_dir / "signals.jsonl").exists()


def test_invalid_json_rejected(processor, write_artifact, cfg):
    import pathlib
    p = pathlib.Path(cfg.watch_dir)
    p.mkdir(parents=True, exist_ok=True)
    f = p / "BAD_decision.json"
    f.write_text("{not json", encoding="utf-8")
    res = processor.process(f)
    assert res.kind == "invalid"
    audit = AuditChain(cfg.audit_file, cfg.now)
    assert any(r["kind"] == "rejected_invalid" for r in audit.read())


def test_future_effective_date_blocked(processor, write_artifact):
    doc = build_sample(effective_date=date.today() + timedelta(days=1))
    res = processor.process(write_artifact(doc))
    assert res.kind == "blocked"
    assert any("future" in r for r in res.reasons)


def test_blocked_gates_no_signal(processor, write_artifact, transport_state, cfg):
    transport_state["account"]["cash"] = 1000.0  # below 25k reserve
    res = processor.process(write_artifact(build_sample()))
    assert res.kind == "blocked"
    assert any("cash" in r for r in res.reasons)
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    assert len(store.read_all()) == 0


def test_reference_unavailable_blocks(processor, write_artifact, transport_state):
    transport_state.pop("quote", None)
    transport_state.pop("account", None)
    res = processor.process(write_artifact(build_sample()))
    assert res.kind == "blocked"
    assert any("reference" in r for r in res.reasons)


def test_kill_switch_halts_and_persists(processor, write_artifact, cfg):
    # halt: sentinel exists -> no emission, episode latched
    cfg.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.kill_switch_path.write_text("", encoding="utf-8")
    res = processor.process(write_artifact(build_sample()))
    assert res.kind == "halted"
    assert is_halted(cfg.kill_switch_path)
    # restart-style: a fresh processor over the same paths still suppressed
    from signald.notifier import Notifier
    from signald.processor import SignalProcessor

    audit2 = AuditChain(cfg.audit_file, cfg.now)
    journal2 = Journal(cfg.journal_file, cfg.now)
    store2 = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    proc2 = SignalProcessor(cfg, processor.mandate, store2, journal2, audit2,
                            processor.ref, Notifier(now=cfg.now))
    assert proc2.process(write_artifact(build_sample())).kind == "halted"
    assert len(store2.read_all()) == 0
    assert any(r["kind"] == "halted" for r in audit2.read())


def test_notifier_down_still_persists(processor, write_artifact, cfg, seam):
    from signald.alpaca_ref import AlpacaReference
    from signald.notifier import Notifier
    from signald.processor import SignalProcessor

    def boom(event):
        raise OSError("webhook unreachable")

    notifier = Notifier(transport=boom, now=cfg.now)
    audit = AuditChain(cfg.audit_file, cfg.now)
    journal = Journal(cfg.journal_file, cfg.now)
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    proc = SignalProcessor(cfg, processor.mandate, store, journal, audit,
                           AlpacaReference(transport=seam), notifier)
    res = proc.process(write_artifact(build_sample()))
    assert res.kind == "emitted"
    assert len(store.read_all()) == 1  # journal-first; notifier may fail
    assert any(r["kind"] == "notifier_failed" for r in audit.read())


def test_envelope_carries_config_hash_and_approval(processor, write_artifact, cfg):
    res = processor.process(write_artifact(build_sample()))
    assert res.kind == "emitted"
    assert res.envelope["config_hash"] == cfg.config_hash()
    assert res.envelope["approval"]["state"] == "not_required"
