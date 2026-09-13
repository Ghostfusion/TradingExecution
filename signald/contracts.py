"""Versioned envelope contract (plan §2): negotiation, closed enums, expiry, dead-letter.

The research → execution boundary is typed data, never prose. This module is
the only place that decides whether a dropped artifact may be ingested:

* `schema_version` is semver; an unknown **MAJOR** is rejected, never coerced;
* enums (`action`, `direction`, `trade_permission`, `binding_gate`, `sleeve`)
  are **closed** — an unknown member is a rejection, not a default;
* `expires_at` is mandatory for schema ≥ 1.1.0 and enforced against the
  injected clock;
* `artifact_sha256` (when declared) must match the artifact body, so a file
  edited in transit cannot be ingested as if it were produced.

Every rejection carries a machine-readable `reason_code`, is written to the
dead-letter directory with a `.reason.json` sidecar, and is audited. Failures
are never silent and never partially applied.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .schema import (
    DIRECTION_TO_ACTION,
    RATING_TO_DIRECTION,
    VALID_DATA_QUALITY,
    VALID_DIRECTIONS,
    sha256_of,
)

#: Current producer/consumer contract.
SCHEMA_VERSION = "1.1.0"
SUPPORTED_MAJOR = (1,)

#: Gate check names, most-binding first (design §4.2). `binding_gate` must be a
#: member; the house gate owns the logic, this tuple owns the vocabulary.
GATE_PRECEDENCE: tuple[str, ...] = (
    "mandate",
    "sleeve_capital",
    "house_drawdown",
    "house_cvar",
    "correlation_stress",
    "vol_regime",
    "market_regime",
    "knife_guard",
    "concentration",
    "liquidity",
    "cost",
    "wash",
    "shortability",
    "data",
    "time",
    "halt",
    "approval",
)

TRADE_PERMISSIONS = ("ALLOW", "REDUCE", "BLOCK")
SLEEVES = ("swing", "intraday")
VALID_CALIBERS_NOTE = "free text; 'adjusted'/'unadjusted'/'mixed'/'unknown' are the known values"


class EnvelopeError(ValueError):
    """Artifact/envelope rejected at the boundary (fail closed)."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def parse_semver(value: Any) -> tuple[int, int, int]:
    """Parse ``MAJOR.MINOR.PATCH``; a bare int is the legacy ``1.0.0``."""
    if isinstance(value, bool):  # bool is an int subclass - never a version
        raise EnvelopeError("invalid_version", f"schema_version is not a version: {value!r}")
    if isinstance(value, int):
        return (value, 0, 0)
    text = str(value or "").strip()
    if not text:
        raise EnvelopeError("invalid_version", "schema_version is missing")
    parts = text.split(".")
    if len(parts) > 3 or not all(p.isdigit() for p in parts):
        raise EnvelopeError("invalid_version", f"schema_version is not semver: {value!r}")
    major, minor, patch = (int(p) for p in (parts + ["0", "0", "0"])[:3])
    return (major, minor, patch)


def check_compatibility(value: Any) -> tuple[int, int, int]:
    """Negotiate the producer version against this consumer (BACKWARD_TRANSITIVE)."""
    parsed = parse_semver(value)
    if parsed[0] not in SUPPORTED_MAJOR:
        raise EnvelopeError(
            "unknown_major",
            f"schema_version {value!r} major {parsed[0]} unsupported "
            f"(supported: {list(SUPPORTED_MAJOR)})",
        )
    return parsed


def is_v11(version: tuple[int, int, int]) -> bool:
    """True when the artifact claims the 1.1 producer contract (strict rules)."""
    return version >= (1, 1, 0)


def _require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if value is None or not str(value).strip():
        raise EnvelopeError("missing_field", f"required field {key!r} is missing or empty")
    return str(value).strip()


def _parse_iso(value: Any, field: str, *, require_offset: bool = False) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise EnvelopeError("missing_field", f"{field} is missing")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EnvelopeError("invalid_timestamp", f"{field} is not ISO-8601: {value!r}") from exc
    if require_offset and stamp.tzinfo is None:
        # An offset-free stamp is ambiguous: naive-to-UTC vs naive-to-local
        # differ by the host's offset, which is exactly how an expiry is
        # silently wrong by hours. Fail closed instead of guessing.
        raise EnvelopeError(
            "naive_timestamp",
            f"{field}={value!r} carries no UTC offset (RFC 3339 with an offset is required)",
        )
    return stamp


def _check_closed(value: Any, allowed: tuple[str, ...], field: str) -> str | None:
    """Closed-enum check; returns the normalised member or None when absent."""
    if value is None:
        return None
    member = str(value).strip()
    if member not in allowed:
        raise EnvelopeError(
            "invalid_enum", f"{field}={value!r} is not one of {list(allowed)}"
        )
    return member


def verify_artifact_hash(raw: dict[str, Any]) -> None:
    """Recompute `artifact_sha256` over the body (all keys but the two hashes).

    Both declared hashes are excluded so `artifact_sha256` and `decision_hash`
    are each computable in either order - the same idiom as the mandate's
    self-exclusion, applied to two fields.
    """
    declared = str(raw.get("artifact_sha256") or "").strip()
    if not declared:
        raise EnvelopeError("missing_field", "artifact_sha256 is missing")
    body = {k: v for k, v in raw.items() if k not in ("artifact_sha256", "decision_hash")}
    computed = sha256_of(body)
    if declared.removeprefix("sha256:") != computed:
        raise EnvelopeError(
            "artifact_hash_mismatch",
            "artifact_sha256 does not match the artifact body (edited in transit?)",
        )


def validate_envelope(raw: dict[str, Any], *, now: datetime) -> tuple[int, int, int]:
    """Full boundary validation. Raises :class:`EnvelopeError`; returns the version.

    Structural only: no action resolution beyond the enum/rating mapping, no
    numeric coercion of the body (``parse_research_decision`` owns that).
    """
    if not isinstance(raw, dict):  # runtime guard: the decoder may hand us a list
        raise EnvelopeError(
            "not_an_object", f"artifact must be a JSON object, got {type(raw).__name__}"
        )

    version = check_compatibility(raw.get("schema_version", 1))

    _require_str(raw, "ticker")

    # closed enums
    _check_closed(raw.get("direction"), tuple(sorted(VALID_DIRECTIONS)), "direction")
    _check_closed(raw.get("action"), ("BUY", "HOLD", "REDUCE", "EXIT", "NONE"), "action")
    _check_closed(raw.get("trade_permission"), TRADE_PERMISSIONS, "trade_permission")
    if raw.get("trade_permission") is not None:
        raise EnvelopeError(
            "producer_set_permission",
            "trade_permission is gate-owned (design §4.3): a producer artifact may not set it",
        )
    # `binding_gate` on an INGESTED artifact is display data, not authority: the
    # research layer already emits it as an advisory label with its own
    # vocabulary ("book_drawdown", "analyzed_name_cvar", ...). The gate always
    # computes its own binding gate, so the producer's value is ignored here -
    # rejecting it would break the research feed over a collision of names.
    # The execution-owned envelope enforces GATE_PRECEDENCE when it is emitted.
    _check_closed(raw.get("sleeve"), SLEEVES, "sleeve")
    _check_closed(
        str(raw.get("data_quality")).strip().lower() if raw.get("data_quality") else None,
        tuple(sorted(VALID_DATA_QUALITY)),
        "data_quality",
    )

    # action must resolve *somehow* (mirrors ResearchDecision.action)
    direction = str(raw.get("direction") or "").strip().lower()
    rating = str(raw.get("rating") or "").strip().lower()
    if direction not in DIRECTION_TO_ACTION and rating not in RATING_TO_DIRECTION:
        raise EnvelopeError(
            "unresolvable_action",
            f"cannot resolve action from direction={raw.get('direction')!r} "
            f"rating={raw.get('rating')!r}",
        )

    score = raw.get("opportunity_score")
    if score is not None:
        try:
            score_f = float(score)
        except (TypeError, ValueError) as exc:
            raise EnvelopeError(
                "invalid_opportunity_score", f"opportunity_score is not a number: {score!r}"
            ) from exc
        if not 0.0 <= score_f <= 100.0:
            raise EnvelopeError(
                "invalid_opportunity_score",
                f"opportunity_score {score_f} outside 0..100 (producer-owned scale)",
            )

    strict_stamps = is_v11(version)  # an offset-free stamp is not acceptable on the wire
    expired = (
        _parse_iso(raw.get("expires_at"), "expires_at", require_offset=strict_stamps)
        if raw.get("expires_at")
        else None
    )
    produced_at = raw.get("produced_at")

    if is_v11(version):
        for key in ("expires_at", "idempotency_key", "producer", "artifact_sha256"):
            if not raw.get(key):
                raise EnvelopeError(
                    "missing_field",
                    f"{key} is mandatory for schema_version {SCHEMA_VERSION} (fail closed)",
                )
        key = str(raw["idempotency_key"]).strip()
        try:
            parsed_key = uuid.UUID(key)
        except (ValueError, AttributeError, TypeError) as exc:
            raise EnvelopeError(
                "invalid_idempotency_key", f"idempotency_key is not a UUID: {key!r}"
            ) from exc
        if parsed_key.version != 4:
            raise EnvelopeError(
                "invalid_idempotency_key",
                f"idempotency_key must be UUIDv4, got version {parsed_key.version}",
            )
        producer = raw["producer"]
        if not isinstance(producer, dict) or not str(producer.get("service") or "").strip():
            raise EnvelopeError(
                "invalid_producer", "producer must be an object carrying a non-empty 'service'"
            )
        verify_artifact_hash(raw)

    if expired is not None:
        produced = (
            _parse_iso(produced_at, "produced_at", require_offset=strict_stamps)
            if produced_at
            else None
        )
        if produced is not None and expired < produced:
            raise EnvelopeError(
                "invalid_timestamp", f"expires_at {expired.isoformat()} precedes produced_at"
            )
        if _as_aware(now) > _as_aware(expired):
            stamp = now.isoformat(timespec="seconds")
            raise EnvelopeError(
                "expired", f"artifact expired at {expired.isoformat()} (now {stamp})"
            )

    return version


def _as_aware(value: datetime) -> datetime:
    """Normalise to UTC-aware so an expiry comparison is never off by an offset.

    A naive value is read as the *host's local* time - that is what the clock
    seam (`Config.now_fn`, defaulting to `datetime.now()`) produces, and reading
    it as UTC instead silently shifts an expiry by the host offset. Envelope
    timestamps must carry an explicit offset, so they never rely on this.
    """
    from datetime import UTC

    return value.astimezone(UTC)


@dataclass(frozen=True)
class DeadLetter:
    """Result of quarantining one rejected artifact."""

    path: Path
    reason_path: Path
    reason_code: str
    detail: str


def dead_letter(
    raw: Any,
    error: EnvelopeError,
    *,
    source: str | Path,
    dir_path: str | Path,
    now: datetime,
    producer_hint: str | None = None,
) -> DeadLetter:
    """Copy the rejected artifact + a `.reason.json` sidecar into the quarantine dir."""
    target_dir = Path(dir_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    src = Path(source)
    stem = f"{src.stem}-{stamp}-{error.reason_code}"
    payload = target_dir / f"{stem}.json"
    payload.write_text(json.dumps(raw, indent=2, sort_keys=True, default=str), encoding="utf-8")
    reason = {
        "reason_code": error.reason_code,
        "detail": error.detail,
        "received_at": now.isoformat(timespec="seconds"),
        "source": str(src),
        "producer": producer_hint,
    }
    reason_path = target_dir / f"{stem}.reason.json"
    reason_path.write_text(json.dumps(reason, indent=2, sort_keys=True), encoding="utf-8")
    return DeadLetter(payload, reason_path, error.reason_code, error.detail)
