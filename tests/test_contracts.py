"""P0 contract tests: version negotiation, closed enums, expiry, hash pinning.

These are the boundary tests the phase gate names: an unknown MAJOR is
rejected, an unknown enum member is rejected (never coerced), an expired
artifact is rejected, and a tampered artifact is rejected by hash.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from signald.contracts import (
    SCHEMA_VERSION,
    EnvelopeError,
    check_compatibility,
    dead_letter,
    parse_semver,
    validate_envelope,
    verify_artifact_hash,
)
from signald.samples import build_sample, build_sample_v11

pytestmark = pytest.mark.timeout(120)

NOW = datetime(2026, 9, 12, 14, 0)


def aware(stamp: datetime) -> datetime:
    """The fixture clock is naive-local (like `Config.now_fn`); stamp it aware."""
    return stamp.astimezone(UTC)


def v11(**overrides) -> dict:
    return build_sample_v11(effective_date=NOW.date(), **overrides)


# --- version negotiation ---------------------------------------------------
def test_legacy_integer_version_is_supported():
    assert check_compatibility(1) == (1, 0, 0)
    assert parse_semver("1.1.0") == (1, 1, 0)
    assert parse_semver("1.2") == (1, 2, 0)


def test_unknown_major_is_rejected():
    with pytest.raises(EnvelopeError) as exc:
        check_compatibility("2.0.0")
    assert exc.value.reason_code == "unknown_major"


@pytest.mark.parametrize("bad", ["", "one.two.three", "1.x.0", True, "1.0.0.0"])
def test_malformed_version_is_rejected(bad):
    with pytest.raises(EnvelopeError) as exc:
        parse_semver(bad)
    assert exc.value.reason_code == "invalid_version"


def test_minor_bump_within_a_major_is_accepted():
    """BACKWARD_TRANSITIVE: a newer MINOR must still be readable."""
    validate_envelope(_rehash(v11(schema_version="1.7.3")), now=NOW)
    validate_envelope(_rehash(v11(schema_version="1.1.9")), now=NOW)


def _rehash(doc: dict) -> dict:
    """Recompute both hashes after a mutation (test-only producer helper)."""
    from signald.schema import sha256_of

    doc.pop("artifact_sha256", None)
    doc.pop("decision_hash", None)
    doc["artifact_sha256"] = sha256_of(
        {k: v for k, v in doc.items() if k not in ("artifact_sha256", "decision_hash")}
    )
    doc["decision_hash"] = "sha256:" + sha256_of(
        {k: v for k, v in doc.items() if k != "decision_hash"}
    )
    return doc


# --- closed enums ----------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "bad", "code"),
    [
        ("direction", "accumulate", "invalid_enum"),
        ("action", "STRONG_BUY", "invalid_enum"),
        ("trade_permission", "MAYBE", "invalid_enum"),
        ("sleeve", "scalping", "invalid_enum"),
        ("data_quality", "very_fresh", "invalid_enum"),
    ],
)
def test_unknown_enum_members_are_rejected(field, bad, code):
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(v11(**{field: bad})), now=NOW)
    assert exc.value.reason_code == code
    assert field in exc.value.detail


def test_an_ingested_binding_gate_is_display_data_not_an_enum_claim():
    """The research layer emits its own advisory binding label.

    Its vocabulary ("book_drawdown", "analyzed_name_cvar", ...) is not the
    gate's. Rejecting the artifact over a collision of names would kill the
    research feed; the gate computes its own binding gate and ignores this one.
    """
    for label in ("book_drawdown", "analyzed_name_cvar", "risk_gate", "halt"):
        validate_envelope(_rehash(v11(binding_gate=label)), now=NOW)


def test_a_bad_sleeve_on_ingest_is_still_rejected():
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(v11(sleeve="scalping")), now=NOW)
    assert exc.value.reason_code == "invalid_enum"


def test_a_producer_may_not_set_the_gate_owned_permission():
    """Design D4: opportunity_score is producer-owned, trade_permission is not."""
    doc = v11(trade_permission="ALLOW")
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(doc), now=NOW)
    assert exc.value.reason_code == "producer_set_permission"


def test_unresolvable_action_is_rejected():
    doc = v11(direction=None, rating=None)
    doc["rating"] = None
    doc["direction"] = None
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(doc), now=NOW)
    assert exc.value.reason_code == "unresolvable_action"


# --- expiry ----------------------------------------------------------------
def test_artifact_expiring_later_today_is_accepted():
    validate_envelope(v11(), now=NOW)


def test_expired_artifact_is_rejected():
    doc = v11(expires_at=aware(NOW) - timedelta(minutes=1))
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(doc), now=NOW)
    assert exc.value.reason_code == "expired"


def test_expiry_before_production_is_rejected():
    doc = v11(produced_at=aware(NOW), expires_at=aware(NOW) - timedelta(hours=2))
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(doc), now=NOW)
    assert exc.value.reason_code == "invalid_timestamp"


def test_v11_requires_an_expiry():
    doc = v11()
    doc.pop("expires_at")
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(doc), now=NOW)
    assert exc.value.reason_code == "missing_field"
    assert "expires_at" in exc.value.detail


def test_a_naive_timestamp_is_rejected_on_the_wire():
    """A naive stamp is ambiguous (UTC vs host-local): fail closed, do not guess."""
    doc = v11(expires_at=datetime(2026, 9, 12, 20, 0))
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(doc), now=NOW)
    assert exc.value.reason_code == "naive_timestamp"


def test_legacy_v1_artifact_needs_no_expiry():
    """Phase-A artifacts keep working: the strict rules are version-gated."""
    validate_envelope(build_sample(effective_date=NOW.date()), now=NOW)


# --- provenance + hash pinning --------------------------------------------
def test_v11_requires_idempotency_key_and_producer():
    for field in ("idempotency_key", "producer"):
        doc = v11()
        doc.pop(field)
        with pytest.raises(EnvelopeError) as exc:
            validate_envelope(_rehash(doc), now=NOW)
        assert exc.value.reason_code == "missing_field", field
        assert field in exc.value.detail


def test_v11_requires_the_artifact_hash():
    doc = v11()
    doc.pop("artifact_sha256")
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(doc, now=NOW)
    assert exc.value.reason_code == "missing_field"
    assert "artifact_sha256" in exc.value.detail


def test_idempotency_key_must_be_uuid4():
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(v11(idempotency_key="not-a-uuid")), now=NOW)
    assert exc.value.reason_code == "invalid_idempotency_key"

    legacy = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"  # v1 UUID
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(v11(idempotency_key=legacy)), now=NOW)
    assert exc.value.reason_code == "invalid_idempotency_key"


def test_producer_must_name_a_service():
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(v11(producer={"git_sha": "abc"})), now=NOW)
    assert exc.value.reason_code == "invalid_producer"


def test_tampered_body_fails_the_artifact_hash():
    doc = v11()
    doc["thesis"] = "silently rewritten after production"
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(doc, now=NOW)
    assert exc.value.reason_code == "artifact_hash_mismatch"


def test_declared_hash_is_recomputed_not_trusted():
    doc = v11()
    doc["artifact_sha256"] = "0" * 64
    with pytest.raises(EnvelopeError) as exc:
        verify_artifact_hash(doc)
    assert exc.value.reason_code == "artifact_hash_mismatch"


def test_the_two_hash_orders_are_independent():
    """artifact_sha256 ignores decision_hash and vice versa (no circularity)."""
    doc = v11()
    before = doc["artifact_sha256"]
    doc["decision_hash"] = "sha256:" + "a" * 64  # nonsense but not covered by artifact hash
    verify_artifact_hash(doc)
    assert doc["artifact_sha256"] == before


# --- opportunity score -----------------------------------------------------
@pytest.mark.parametrize("bad", [101, -0.5, "high"])
def test_opportunity_score_range_is_enforced(bad):
    with pytest.raises(EnvelopeError) as exc:
        validate_envelope(_rehash(v11(opportunity_score=bad)), now=NOW)
    assert exc.value.reason_code == "invalid_opportunity_score"


# --- dead-letter -----------------------------------------------------------
def test_dead_letter_writes_payload_and_reason_sidecar(tmp_path):
    source = tmp_path / "decisions" / "NVDA.json"
    source.parent.mkdir()
    doc = {"ticker": "NVDA", "schema_version": "9.9.9"}
    source.write_text(json.dumps(doc), encoding="utf-8")

    result = dead_letter(
        doc,
        EnvelopeError("unknown_major", "major 9 unsupported"),
        source=source,
        dir_path=tmp_path / "dead_letter",
        now=NOW,
        producer_hint="tradingagents",
    )

    assert result.path.exists() and result.reason_path.exists()
    assert "unknown_major" in result.path.name
    reason = json.loads(result.reason_path.read_text(encoding="utf-8"))
    assert reason["reason_code"] == "unknown_major"
    assert reason["source"].endswith("NVDA.json")
    assert reason["producer"] == "tradingagents"
    assert json.loads(result.path.read_text(encoding="utf-8")) == doc


def test_schema_version_constant_is_what_the_producer_sample_claims():
    assert SCHEMA_VERSION == "1.1.0"
    assert v11()["schema_version"] == SCHEMA_VERSION
