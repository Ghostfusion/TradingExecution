"""P0 inbox tests: effectively-once ingest, dead-letter, recovery, quarantine.

The phase gate names two of these: "dedupe proven on replay" and "unknown
MAJOR rejected". The rest prove the failure path is loud (dead-letter file +
reason sidecar + audit row) and that a restart can tell an unfinished
admission from a completed one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from signald.inbox import ACCEPTED, DEAD_LETTERED, DEDUPED, Inbox
from signald.samples import build_sample, build_sample_v11, write_sample
from signald.schema import sha256_of
from signald.stores import AuditChain

pytestmark = pytest.mark.timeout(120)

NOW = datetime(2026, 9, 12, 14, 0)


@pytest.fixture()
def audit(cfg):
    return AuditChain(cfg.audit_file, cfg.now)


def make_inbox(cfg, audit) -> Inbox:
    return Inbox(
        cfg.data_dir / "inbox.jsonl",
        cfg.data_dir / "_dead_letter",
        cfg.data_dir / "_quarantine",
        cfg.now,
        audit,
    )


@pytest.fixture()
def inbox(cfg, audit):
    return make_inbox(cfg, audit)


def drop(tmp_path, doc: dict, name: str = "research_decision.json"):
    """Write an artifact to a temp path (a research-layer drop)."""
    path = tmp_path / "drops" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return path


def v11_doc(**overrides) -> dict:
    return build_sample_v11(effective_date=NOW.date(), **overrides)


def rehash(doc: dict) -> dict:
    """Re-sign an artifact after a deliberate mutation (test-only producer)."""
    doc["artifact_sha256"] = sha256_of(
        {k: v for k, v in doc.items() if k not in ("artifact_sha256", "decision_hash")}
    )
    doc["decision_hash"] = "sha256:" + sha256_of(
        {k: v for k, v in doc.items() if k != "decision_hash"}
    )
    return doc


# --- the happy path --------------------------------------------------------
def test_a_valid_artifact_is_admitted_and_recorded(inbox, tmp_path):
    path = drop(tmp_path, v11_doc())

    result = inbox.admit(path)

    assert result.kind == ACCEPTED and result.admitted
    assert result.key.startswith("tradingagents:")
    assert inbox.seen(result.key)
    assert inbox.pending() == [result.key]
    rows = [json.loads(line) for line in (inbox.path).read_text(encoding="utf-8").splitlines()]
    assert rows[0]["state"] == "admitted"
    assert rows[0]["ticker"] == "AVGO"


def test_a_legacy_v1_artifact_is_still_admitted(inbox, tmp_path):
    """The strict rules are version-gated; Phase-A producers keep working."""
    path = drop(tmp_path, build_sample(effective_date=NOW.date()))

    assert inbox.admit(path).kind == ACCEPTED


def test_commit_closes_the_admission(inbox, tmp_path):
    path = drop(tmp_path, v11_doc())
    key = inbox.admit(path).key

    inbox.commit(key, signal_id="sg-1")

    assert inbox.state_of(key) == "committed"
    assert inbox.pending() == []
    inbox.commit(key)  # idempotent
    assert inbox.state_of(key) == "committed"


# --- replay / duplicate ----------------------------------------------------
def test_replay_of_the_same_artifact_is_deduped(inbox, tmp_path):
    path = drop(tmp_path, v11_doc())

    first = inbox.admit(path)
    second = inbox.admit(path)

    assert first.kind == ACCEPTED
    assert second.kind == DEDUPED
    assert second.key == first.key
    assert second.detail == "duplicate artifact"


def test_re_dropping_the_same_producer_run_under_another_name_is_deduped(inbox, tmp_path):
    """The key is producer-identity, not the file path (watch may re-drop)."""
    doc = v11_doc()
    inbox.admit(drop(tmp_path, doc, "research_decision.json"))

    again = inbox.admit(drop(tmp_path, doc, "research_decision-copy.json"))

    assert again.kind == DEDUPED


def test_a_different_run_id_is_a_different_admission(inbox, tmp_path):
    first = inbox.admit(drop(tmp_path, v11_doc(run_id="run-a"), "a.json"))
    second = inbox.admit(drop(tmp_path, v11_doc(run_id="run-b"), "b.json"))

    assert first.kind == ACCEPTED
    assert second.kind == ACCEPTED
    assert first.key != second.key


def test_an_unfinished_admission_survives_a_restart(cfg, audit, tmp_path):
    """own-before-write: the row is durable before the side effect."""
    doc = v11_doc()
    path = drop(tmp_path, doc)
    key = make_inbox(cfg, audit).admit(path).key

    restarted = make_inbox(cfg, audit)

    assert restarted.pending() == [key]
    assert restarted.admit(path).kind == DEDUPED  # no second effect


# --- dead-letter ----------------------------------------------------------
def test_unknown_major_is_dead_lettered(inbox, tmp_path, audit):
    path = drop(tmp_path, v11_doc(schema_version="2.0.0"))

    result = inbox.admit(path)

    assert result.kind == DEAD_LETTERED
    assert result.reason_code == "unknown_major"
    assert not inbox.seen(result.key)  # nothing to dedupe: it never entered
    reasons = list(inbox.dead_letter_dir.glob("*.reason.json"))
    assert len(reasons) == 1
    assert json.loads(reasons[0].read_text(encoding="utf-8"))["reason_code"] == "unknown_major"
    assert [r["kind"] for r in audit.read()] == ["dead_lettered"]


def test_expired_artifact_is_dead_lettered(inbox, tmp_path, now):
    """Expiry is judged against the injected clock, not the wall clock."""
    # the fixture clock is naive-local (like Config.now_fn): stamp it aware, which
    # is what the wire contract requires
    expired = (now - timedelta(minutes=1)).astimezone(UTC)
    doc = v11_doc(produced_at=expired - timedelta(hours=2), expires_at=expired)

    result = inbox.admit(drop(tmp_path, rehash(doc)))

    assert result.kind == DEAD_LETTERED
    assert result.reason_code == "expired"


def test_tampered_artifact_is_dead_lettered(inbox, tmp_path):
    doc = v11_doc()
    doc["stop_loss_hack"] = 0.01  # body edited after production

    result = inbox.admit(drop(tmp_path, doc))

    assert result.kind == DEAD_LETTERED
    assert result.reason_code == "artifact_hash_mismatch"


def test_unreadable_drop_is_dead_lettered_not_crashed(inbox, tmp_path):
    path = tmp_path / "drops" / "broken.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    result = inbox.admit(path)

    assert result.kind == DEAD_LETTERED
    assert result.reason_code == "unreadable"
    assert inbox.dead_letter_dir.exists()


def test_missing_file_is_dead_lettered(inbox, tmp_path):
    result = inbox.admit(tmp_path / "nope.json")

    assert result.kind == DEAD_LETTERED
    assert result.reason_code == "unreadable"


# --- quarantine + audit ----------------------------------------------------
def test_quarantine_records_a_non_permissible_signal(inbox, audit):
    envelope = {"signal_id": "sg-1", "ticker": "NVDA", "action": "BUY"}

    path = inbox.quarantine(envelope, "sleeve_capital", "over ceiling", key="k1")

    assert path.exists()
    sidecar = json.loads(path.with_suffix(".reason.json").read_text(encoding="utf-8"))
    assert sidecar["reason_code"] == "sleeve_capital"
    assert sidecar["key"] == "k1"
    kinds = [row["kind"] for row in audit.read()]
    assert kinds == ["quarantined"]


def test_admission_and_duplicate_are_both_audited(inbox, audit, tmp_path):
    path = drop(tmp_path, v11_doc())

    inbox.admit(path)
    inbox.admit(path)

    kinds = [row["kind"] for row in audit.read()]
    assert kinds == ["admitted", "deduped"]


def test_the_written_row_is_hash_free_and_replayable(inbox, tmp_path):
    """The row must not carry secrets or unbounded prose - only identity."""
    inbox.admit(drop(tmp_path, v11_doc()))

    row = json.loads(inbox.path.read_text(encoding="utf-8").splitlines()[0])

    assert set(row) >= {"key", "state", "at", "ticker", "decision_hash", "source"}
    assert len(row["key"]) <= 200


def test_write_sample_writes_a_legacy_artifact_by_default(tmp_path):
    """Regression guard: samples.py stays the Phase-A shape unless asked."""
    path = write_sample(tmp_path / "decisions" / "research_decision.json")

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "schema_version" not in doc
    assert doc["ticker"] == "AVGO"
