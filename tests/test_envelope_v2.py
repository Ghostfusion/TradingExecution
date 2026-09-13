"""The v2 envelope and the ingest boundary, proven through the real processor.

P1's exit criteria include "the envelope carries the two orthogonal fields" and
P0's include "dedupe proven on replay". Both are only true if they hold on the
*production* path, so these tests drive `SignalProcessor` with a real `Inbox`
over a `tmp_path` tree - no mocks of the code under test.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from signald.alpaca_ref import AlpacaReference
from signald.inbox import Inbox
from signald.processor import SignalProcessor
from signald.samples import build_sample, build_sample_v11
from signald.stores import AuditChain, Journal, SignalStore

pytestmark = pytest.mark.timeout(120)

V2_KEYS = {
    "sleeve",
    "opportunity_score",
    "trade_permission",
    "binding_gate",
    "permission_reason_code",
    "permission_reason",
    "idempotency_key",
    "stop_kind",
    "schema_version",
}
PHASE_A_KEYS = {
    "signal_id",
    "decision_hash",
    "ticker",
    "action",
    "target_pct",
    "score",
    "confidence",
    "stop",
    "take_profit",
    "expiry",
    "ref",
    "expected_cost_band_bps",
    "gates",
    "approval",
    "emitted_at",
    "config_hash",
    "commit",
}


def make_inbox(cfg, audit) -> Inbox:
    return Inbox(
        cfg.data_dir / "inbox.jsonl",
        cfg.data_dir / "_dead_letter",
        cfg.data_dir / "_quarantine",
        cfg.now,
        audit,
    )


@pytest.fixture()
def wired(cfg, mandate, seam, notifier):
    """A processor with the ingest boundary installed (the production shape)."""
    audit = AuditChain(cfg.audit_file, cfg.now)
    store = SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json")
    journal = Journal(cfg.journal_file, cfg.now)
    return SignalProcessor(
        cfg,
        mandate,
        store,
        journal,
        audit,
        AlpacaReference(transport=seam),
        notifier,
        make_inbox(cfg, audit),
    )


def v11(**kw):
    kw.setdefault("effective_date", date.today())
    return build_sample_v11(**kw)


def signals(cfg):
    return SignalStore(cfg.data_dir / "signals.jsonl", cfg.data_dir / "latest.json").read_all()


# --- the envelope ----------------------------------------------------------
def test_a_v1_1_artifact_gets_the_v2_fields_additively(wired, write_artifact):
    res = wired.process(write_artifact(v11()))

    assert res.kind == "emitted", res.reasons
    env = res.envelope
    assert set(env) >= V2_KEYS, "v2 fields missing"
    assert set(env) >= PHASE_A_KEYS, "a Phase-A consumer would break"
    assert env["sleeve"] == "swing"  # the router owns the sleeve
    assert env["opportunity_score"] == 42.0  # producer-owned, passed through
    assert env["trade_permission"] in {"ALLOW", "REDUCE"}
    assert env["schema_version"] == "1.1.0"
    assert env["idempotency_key"]
    assert env["stop_kind"] == "native"
    assert env["gates"]["verdict"] in {"PASS", "DOWNGRADE"}  # Phase-A vocabulary kept


def test_a_legacy_v1_artifact_still_emits(wired, write_artifact):
    res = wired.process(write_artifact(build_sample()))

    assert res.kind == "emitted", res.reasons
    assert res.envelope["sleeve"] == "swing"
    assert res.envelope["opportunity_score"] is None


def test_permission_does_not_follow_the_opportunity_score(wired, write_artifact, seam):
    """Design D4: the two fields answer different questions, independently."""
    low_score_clean_book = wired.process(write_artifact(v11(opportunity_score=5.0)))
    assert low_score_clean_book.kind == "emitted"
    assert low_score_clean_book.envelope["trade_permission"] in {"ALLOW", "REDUCE"}

    seam.state["account"]["cash"] = 1_000.0  # below the reserve -> the book forbids
    high_score_blocked = wired.process(write_artifact(v11(opportunity_score=99.0, run_id="b")))

    assert high_score_blocked.kind == "blocked"
    assert high_score_blocked.envelope is None


# --- the ingest boundary ---------------------------------------------------
def test_a_replay_cannot_emit_twice(wired, write_artifact, cfg):
    path = write_artifact(v11())

    assert wired.process(path).kind == "emitted"
    assert wired.process(path).kind == "skipped_duplicate"
    assert len(signals(cfg)) == 1


def test_a_successful_emit_commits_its_admission(wired, write_artifact):
    path = write_artifact(v11())
    raw = json.loads(path.read_text(encoding="utf-8"))
    key = wired.inbox.key_for(raw, path)

    wired.process(path)

    assert wired.inbox.state_of(key) == "committed"
    assert wired.inbox.pending() == []


def test_an_unknown_major_is_dead_lettered_not_processed(wired, write_artifact, cfg):
    res = wired.process(write_artifact(v11(schema_version="2.0.0")))

    assert res.kind == "dead_lettered"
    assert "unknown_major" in res.reasons[0]
    assert signals(cfg) == []
    reasons = list((cfg.data_dir / "_dead_letter").glob("*.reason.json"))
    assert [json.loads(p.read_text(encoding="utf-8"))["reason_code"] for p in reasons] == [
        "unknown_major"
    ]


def test_a_producer_may_not_choose_the_sleeve(wired, write_artifact, cfg):
    res = wired.process(write_artifact(v11(sleeve="intraday")))

    assert res.kind == "blocked"
    assert "sleeve routing refused" in res.reasons[0]
    assert signals(cfg) == []
    assert list((cfg.data_dir / "_quarantine").glob("*.reason.json"))


def test_a_gated_out_signal_lands_in_quarantine(wired, write_artifact, cfg, seam):
    """Produced but not permissible: recorded with the binding gate, never dropped."""
    seam.state["account"]["cash"] = 1_000.0  # the cash-reserve rule blocks

    res = wired.process(write_artifact(v11()))

    assert res.kind == "blocked"
    reasons = list((cfg.data_dir / "_quarantine").glob("*.reason.json"))
    assert len(reasons) == 1
    sidecar = json.loads(reasons[0].read_text(encoding="utf-8"))
    assert sidecar["reason_code"] == "mandate"
    assert "cash" in sidecar["detail"]


def test_the_notifier_fires_once_per_emitted_signal(wired, write_artifact, webhook_events):
    wired.process(write_artifact(v11()))
    wired.process(write_artifact(v11(run_id="another-run")))

    assert [e["event"] for e in webhook_events] == ["signal", "signal"]


def test_the_audit_chain_records_the_admission_once(wired, write_artifact, cfg):
    wired.process(write_artifact(v11()))

    kinds = [row["kind"] for row in AuditChain(cfg.audit_file, cfg.now).read()]
    assert kinds.count("admitted") == 1  # the boundary
    assert kinds.count("accepted") == 1  # the verdict
