"""Research decision contract + Signal Contract.

The daemon's ONLY input is ``research_decision.json`` (plan §3) emitted by the
research layer. It is parsed, validated, hash-pinned, normalized to an
agnostic ``SignalContract``, and never re-read from prose.

Design invariants: fail closed (an incomplete/unverifiable artifact is
rejected, not guessed at); deterministic (normalisation is a pure function);
no fabrication (advisory text fields are carried, never parsed).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

VALID_DATA_QUALITY = {"fresh", "stale", "partial", "unknown"}
VALID_DIRECTIONS = {"add", "buy", "hold", "reduce", "sell", "exit", "none"}
VALID_ACTIONS = {"BUY", "HOLD", "REDUCE", "EXIT", "NONE"}

# rating -> direction (used when the artifact carries no explicit direction)
RATING_TO_DIRECTION = {
    "buy": "add",
    "overweight": "add",
    "outperform": "add",
    "hold": "hold",
    "underweight": "reduce",
    "underperform": "reduce",
    "sell": "exit",
}

DIRECTION_TO_ACTION = {
    "add": "BUY",
    "buy": "BUY",
    "hold": "HOLD",
    "reduce": "REDUCE",
    "sell": "REDUCE",
    "exit": "EXIT",
    "none": "NONE",
}


class ContractError(ValueError):
    """Artifact failed validation or hash verification (fail closed)."""


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ResearchDecision:
    ticker: str
    effective_date: date
    rating: str | None
    direction: str | None
    data_quality: str
    schema_version: int = 1
    thesis: str | None = None
    rationale: str | None = None
    recommended_allocation_pct: float | None = None
    target_notional: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    size_pct_book: float | None = None
    price_caliber: str | None = None
    invalidations: tuple[str, ...] = ()
    guardrail_reason: str | None = None
    risk_gate_verdict: str | None = None
    risk_gate_reasons: tuple[str, ...] = ()
    disclosure: dict[str, Any] = field(default_factory=dict)
    decision_hash: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def action(self) -> str:
        """Normalised action; raises ContractError when unresolvable."""
        d = (self.direction or "").strip().lower()
        if d in DIRECTION_TO_ACTION:
            return DIRECTION_TO_ACTION[d]
        r = (self.rating or "").strip().lower()
        if r in RATING_TO_DIRECTION:
            return DIRECTION_TO_ACTION[RATING_TO_DIRECTION[r]]
        raise ContractError(
            f"cannot resolve action from direction={self.direction!r} rating={self.rating!r}"
        )

    def implies_short(self) -> bool:
        return False  # Phase A supports no short intents


def _coerce_float(v: Any, name: str) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"field {name} is not a number: {v!r}") from exc


def parse_research_decision(raw: dict[str, Any]) -> ResearchDecision:
    """Validate a raw artifact dict into a ResearchDecision (raises ContractError)."""
    ticker = str(raw.get("ticker") or "").strip().upper()
    if not ticker:
        raise ContractError("missing required field: ticker")

    eff = raw.get("effective_date")
    try:
        effective = date.fromisoformat(str(eff or ""))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"invalid effective_date: {eff!r}") from exc

    rating = str(raw.get("rating") or "").strip() or None
    direction = str(raw.get("direction") or "").strip().lower() or None
    if direction is not None and direction not in VALID_DIRECTIONS:
        raise ContractError(f"invalid direction: {direction!r}")
    # fail-closed: we must be able to resolve an action *somehow*
    resolved = direction or (rating or "").strip().lower()
    if not resolved:
        raise ContractError("missing both direction and rating; cannot resolve action")

    dq = str(raw.get("data_quality") or "unknown").strip().lower()
    if dq not in VALID_DATA_QUALITY:
        raise ContractError(f"invalid data_quality: {dq!r}")

    pos = raw.get("position") or {}
    if not isinstance(pos, dict):
        raise ContractError("position must be an object")

    invalidations = list(raw.get("invalidations") or [])
    if not isinstance(invalidations, list) or not all(isinstance(x, str) for x in invalidations):
        raise ContractError("invalidations must be a list of strings")

    rg = raw.get("risk_gate") or {}
    rg_verdict = str(rg.get("verdict") or "").strip() or None if isinstance(rg, dict) else None
    rg_reasons = list(rg.get("reasons") or []) if isinstance(rg, dict) else []

    body = json.loads(json.dumps(raw, sort_keys=True, default=str))
    body.pop("decision_hash", None)
    computed_hash = sha256_of(body)

    declared = raw.get("decision_hash")
    if declared:
        norm = str(declared).removeprefix("sha256:")
        if norm != computed_hash:
            raise ContractError("decision_hash mismatch: artifact does not match its declared hash")

    extra = {k: v for k, v in raw.items() if k not in {
        "schema_version", "ticker", "effective_date", "rating", "direction",
        "thesis", "rationale", "recommended_allocation_pct", "position",
        "data_quality", "price_caliber", "invalidations", "guardrail_reason",
        "risk_gate", "disclosure", "decision_hash",
    }}

    rd = ResearchDecision(
        ticker=ticker,
        effective_date=effective,
        rating=rating,
        direction=direction,
        data_quality=dq,
        schema_version=int(raw.get("schema_version", 1)),
        thesis=str(raw.get("thesis") or "") or None,
        rationale=str(raw.get("rationale") or "") or None,
        recommended_allocation_pct=_coerce_float(
            raw.get("recommended_allocation_pct"), "recommended_allocation_pct"
        ),
        target_notional=_coerce_float(pos.get("target_notional"), "position.target_notional"),
        stop_loss=_coerce_float(pos.get("stop_loss"), "position.stop_loss"),
        take_profit=_coerce_float(pos.get("take_profit"), "position.take_profit"),
        size_pct_book=_coerce_float(pos.get("size_pct_book"), "position.size_pct_book"),
        price_caliber=str(raw.get("price_caliber") or "").strip() or None,
        invalidations=tuple(invalidations),
        guardrail_reason=str(raw.get("guardrail_reason") or "") or None,
        risk_gate_verdict=rg_verdict,
        risk_gate_reasons=tuple(rg_reasons),
        disclosure=dict(raw.get("disclosure") or {}),
        decision_hash=computed_hash,
        extra=extra,
    )
    rd.action()  # resolve now so a later gate never surprises
    return rd


@dataclass(frozen=True)
class SignalContract:
    """Agnostic, broker-neutral signal (plan §3 Decision B)."""

    symbol: str
    action: str
    target_pct: float
    expiry: str
    decision_hash: str
    timestamp: str
    target_notional_usd: float | None = None
    score: float | None = None
    confidence: float | None = None
    target_price: float | None = None
    stop_price: float | None = None
    max_position_pct: float | None = None
    strategy: str | None = None
    reason: str | None = None

    @property
    def implies_short(self) -> bool:
        return False


def build_signal_contract(
    rd: ResearchDecision,
    expiry: str,
    now: datetime,
    book_equity: float | None,
) -> SignalContract:
    """Pure normalisation: research decision -> agnostic signal contract."""
    action = rd.action()

    pct = rd.recommended_allocation_pct
    if pct is not None:
        if pct > 1.0:  # tolerate "55" meaning 55%
            pct = pct / 100.0
        pct = max(0.0, min(1.0, pct))
    else:
        pct = max(0.0, min(1.0, rd.size_pct_book or 0.0))

    notional = rd.target_notional
    if notional is None and book_equity is not None:
        notional = pct * book_equity

    score = rd.extra.get("score")
    confidence = rd.extra.get("confidence")
    strategy = rd.extra.get("strategy")

    return SignalContract(
        symbol=rd.ticker,
        action=action,
        target_pct=round(pct, 6),
        target_notional_usd=None if notional is None else round(float(notional), 2),
        score=None if score is None else float(score),
        confidence=None if confidence is None else float(confidence),
        target_price=rd.take_profit,
        stop_price=rd.stop_loss,
        max_position_pct=None if rd.size_pct_book is None else round(rd.size_pct_book, 6),
        strategy=None if strategy is None else str(strategy),
        reason=rd.rationale,
        expiry=expiry,
        decision_hash=rd.decision_hash or "",
        timestamp=now.isoformat(timespec="seconds"),
    )
