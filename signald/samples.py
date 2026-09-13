"""Standalone sample artifact generator (acceptance demo + fixture source).

Generates a valid ``research_decision.json`` exactly matching the daemon's
input contract, so the pipeline can be exercised end-to-end before the
research layer ships its own emitter.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time
from pathlib import Path

from .contracts import SCHEMA_VERSION
from .schema import sha256_of

DEFAULT_SAMPLE = {
    "ticker": "AVGO",
    "effective_date": None,  # filled at runtime with today
    "rating": "Underweight",
    "direction": "reduce",
    "thesis": "Demo artifact: bear debate won; momentum broke.",
    "rationale": "Reduce on strength; do not add below 10-EMA.",
    "recommended_allocation_pct": 0.0,
    "position": {
        "target_notional": None,
        "stop_loss": 320.0,
        "take_profit": None,
        "size_pct_book": 0.0242,
    },
    "data_quality": "fresh",
    "price_caliber": "adjusted",
    "invalidations": ["price_stop_loss: breach below 320.0"],
    "guardrail_reason": None,
    "risk_gate": {"verdict": "PASS", "reasons": []},
    "disclosure": {"sources_used": ["eodhd"], "sources_empty": []},
}


def build_sample(
    ticker: str = "AVGO",
    direction: str = "reduce",
    effective_date: date | None = None,
    data_quality: str = "fresh",
    allocation_pct: float | None = None,
    stop_loss: float | None = None,
    **overrides,
) -> dict:
    doc = json.loads(json.dumps(DEFAULT_SAMPLE))
    doc["ticker"] = ticker.upper()
    doc["direction"] = direction
    doc["effective_date"] = (effective_date or date.today()).isoformat()
    doc["data_quality"] = data_quality
    if allocation_pct is not None:
        doc["recommended_allocation_pct"] = allocation_pct
    if stop_loss is not None:
        doc["position"]["stop_loss"] = stop_loss
    doc.update(overrides)
    body = json.loads(json.dumps(doc, sort_keys=True, default=str))
    doc["decision_hash"] = "sha256:" + sha256_of(body)
    return doc


def doc_effective_date(doc: dict) -> date:
    return date.fromisoformat(str(doc["effective_date"]))


def build_sample_v11(
    ticker: str = "AVGO",
    direction: str = "reduce",
    effective_date: date | None = None,
    *,
    produced_at: datetime | None = None,
    expires_at: datetime | None = None,
    run_id: str | None = None,
    opportunity_score: float | None = 42.0,
    data_quality: str = "fresh",
    **overrides,
) -> dict:
    """A schema-1.1.0 artifact: idempotency key, producer block, expiry, both hashes.

    This is the shape the research layer must emit (implementation plan §2.2).
    ``expires_at`` defaults to 20:00 ET-equivalent (naive UTC) on the decision
    date; callers pass an explicit value when the clock matters.
    """
    doc = build_sample(
        ticker=ticker,
        direction=direction,
        effective_date=effective_date,
        data_quality=data_quality,
        **overrides,
    )
    base_date = doc_effective_date(doc)
    # envelope timestamps are RFC 3339 WITH an offset: a naive stamp is
    # ambiguous and the execution layer rejects it (contracts.naive_timestamp)
    produced = produced_at or datetime.combine(base_date, time(13, 45), tzinfo=UTC)
    expires = expires_at or datetime.combine(base_date, time(20, 0), tzinfo=UTC)
    generated = {
        "schema_version": SCHEMA_VERSION,
        "idempotency_key": str(uuid.uuid4()),
        "produced_at": produced.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "producer": {
            "service": "tradingagents",
            "git_sha": "sample000",
            "run_id": run_id or str(uuid.uuid4()),
        },
        "opportunity_score": opportunity_score,
        "risk_context": {
            "regime": "risk-on",
            "research_cvar_975_1d_pct": 1.1,
            "risk_gate": {"verdict": "PASS", "reasons": []},
        },
        "disclosure": {"sources_used": ["eodhd"], "sources_empty": []},
    }
    for key, value in generated.items():
        doc.setdefault(key, value)  # explicit caller values win
    doc["artifact_sha256"] = sha256_of(
        {k: v for k, v in doc.items() if k not in ("artifact_sha256", "decision_hash")}
    )
    doc["decision_hash"] = "sha256:" + sha256_of(
        {k: v for k, v in doc.items() if k != "decision_hash"}
    )
    return doc


def write_sample(path: str | Path, **kwargs) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(build_sample(**kwargs), indent=2, sort_keys=True), encoding="utf-8")
    return p
