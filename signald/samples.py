"""Standalone sample artifact generator (acceptance demo + fixture source).

Generates a valid ``research_decision.json`` exactly matching the daemon's
input contract, so the pipeline can be exercised end-to-end before the
research layer ships its own emitter.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

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


def write_sample(path: str | Path, **kwargs) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(build_sample(**kwargs), indent=2, sort_keys=True), encoding="utf-8")
    return p
