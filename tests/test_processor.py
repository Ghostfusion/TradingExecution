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


def test_a_blocked_artifact_is_refused_once(processor, write_artifact, transport_state,
                                            cfg, webhook_events):
    """A durable refusal is recorded once: one row, one page - not one per poll.

    2026-09-15: an artifact the mandate forbids (NFLX) sits in the reports watch
    tree, which the daemon re-discovers every 10 s. Before the journal wrote
    refusals, that was a rejected audit row plus a Discord error card per cycle
    (4 rows and 4 cards in 44 s of live running).
    """
    transport_state["account"]["cash"] = 1000.0  # below the 25k reserve

    path = write_artifact(build_sample())
    assert processor.process(path).kind == "blocked"
    assert processor.process(path).kind == "skipped_duplicate"

    audit = AuditChain(cfg.audit_file, cfg.now)
    assert len([r for r in audit.read() if r["kind"] == "rejected"]) == 1
    assert len([e for e in webhook_events if e["event"] == "error"]) == 1
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    assert store.read_all() == []


def _candidates(cfg):
    from signald.stores import CandidateStore

    return CandidateStore(cfg.data_dir / "mandate_candidates.jsonl")


def test_a_not_held_buy_outside_the_mandate_is_queued_as_a_candidate(
    processor, write_artifact, cfg, transport_state, webhook_events
):
    """Option A (2026-09-15): research proposes, the operator promotes.

    A valid Buy for a name the mandate does not list and the account does not
    hold becomes a candidate plus one card carrying the promotion command -
    instead of a bare refusal error.
    """
    transport_state["positions"] = {"positions_value": 0.0, "symbols": []}
    path = write_artifact(build_sample(ticker="NFLX", direction=None, rating="Buy"))

    assert processor.process(path).kind == "blocked"

    rows = _candidates(cfg).read_all()
    assert [(r["ticker"], r["rating"], r["action"]) for r in rows] == [("NFLX", "Buy", "BUY")]
    cards = [e for e in webhook_events if e["event"] == "mandate_candidate"]
    assert len(cards) == 1 and cards[0]["command"] == "signald mandate-add NFLX"
    assert not [e for e in webhook_events if e.get("source") == "signal_blocked"]

    assert processor.process(path).kind == "skipped_duplicate"
    assert len(_candidates(cfg).read_all()) == 1


def test_a_held_buy_outside_the_mandate_is_not_a_candidate(
    processor, write_artifact, cfg, transport_state, webhook_events
):
    transport_state["positions"] = {"positions_value": 500.0, "symbols": ["NFLX"]}

    res = processor.process(write_artifact(build_sample(ticker="NFLX", direction=None, rating="Buy")))

    assert res.kind == "blocked"
    assert not _candidates(cfg).read_all()
    assert [e["source"] for e in webhook_events if e["event"] == "error"] == ["signal_blocked"]


def test_unknown_holdings_do_not_make_a_candidate(processor, write_artifact, cfg, webhook_events):
    """The default seam reports no per-symbol detail: not proof, so no offer."""
    res = processor.process(write_artifact(build_sample(ticker="NFLX", direction=None, rating="Buy")))

    assert res.kind == "blocked"
    assert not _candidates(cfg).read_all()
    assert [e["source"] for e in webhook_events if e["event"] == "error"] == ["signal_blocked"]


def test_a_weak_rating_outside_the_mandate_is_not_a_candidate(
    processor, write_artifact, cfg, transport_state
):
    transport_state["positions"] = {"positions_value": 0.0, "symbols": []}

    doc = build_sample(ticker="NFLX", direction=None, rating="Underweight")
    assert processor.process(write_artifact(doc)).kind == "blocked"
    assert not _candidates(cfg).read_all()


def test_a_mandate_add_reopens_a_refused_artifact(processor, write_artifact, cfg):
    """The refusal row expires with the mandate, so `mandate-add` alone revives it."""
    import json

    from signald.mandate import write_mandate

    path = write_artifact(build_sample(ticker="NFLX", direction=None, rating="Buy"))
    assert processor.process(path).kind == "blocked"

    doc = json.loads(cfg.mandate_path.read_text(encoding="utf-8"))
    doc["symbols"]["allowed"] = sorted(set(doc["symbols"]["allowed"]) | {"NFLX"})
    write_mandate(cfg.mandate_path, doc)

    assert processor.refresh_mandate() == "reloaded"
    assert processor.process(path).kind == "emitted"


def test_a_broken_mandate_edit_keeps_the_loaded_one(processor, write_artifact, cfg):
    """Fail closed: an unparseable mandate keeps the loaded one and pages once."""
    cfg.mandate_path.write_text("{not json", encoding="utf-8")

    assert processor.refresh_mandate() == "invalid"
    assert processor.refresh_mandate() == "invalid"

    audit = AuditChain(cfg.audit_file, cfg.now)
    assert len([r for r in audit.read() if r["kind"] == "mandate_reload_failed"]) == 1
    assert processor.process(write_artifact(build_sample())).kind == "emitted"


def test_a_duplicate_artifact_does_not_grow_the_audit_ledger(processor, write_artifact, cfg):
    """The ledger records decisions, not poll cycles: a re-seen artifact is silent."""
    path = write_artifact(build_sample())
    assert processor.process(path).kind == "emitted"

    audit = AuditChain(cfg.audit_file, cfg.now)
    before = len(audit.read())
    assert processor.process(path).kind == "skipped_duplicate"
    assert len(audit.read()) == before


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
