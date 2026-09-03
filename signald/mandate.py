"""Mandate — the user's persisted, hash-pinned safety contract (plan §4.3).

Rules (from Master_deign.md §3): auto-expiry on every gate tick; any
``allowed`` edit requires a re-sign (hash change) and archives the old
mandate id — old mandates are never mutated; ``shorts:false`` rejects short
intents regardless of research.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .schema import sha256_of

DEFAULT_MANDATE = {
    "id": "mandate-phaseA",
    "owner": "vince",
    "created": "2026-09-03",
    "expires": "2027-01-01",
    "symbols": {
        "allowed": ["SPY", "GOOG", "GLD", "NVDA", "MSFT", "AVGO", "QCOM", "BAC"],
        "shorts": False,
    },
    "max_notional_per_order_usd": 20000,
    "max_total_exposure_usd": 150000,
    "min_cash_reserve_usd": 25000,
    "max_daily_trades": 12,
    "daily_loss_limit_usd": 5000,
    "approval_mode": "manual-high-order",
    "kill_switch_path": None,
}


class MandateError(ValueError):
    pass


@dataclass(frozen=True)
class Mandate:
    id: str
    owner: str
    created: date
    expires: date
    allowed: frozenset[str]
    shorts: bool
    max_notional_per_order_usd: float
    max_total_exposure_usd: float
    min_cash_reserve_usd: float
    max_daily_trades: int
    daily_loss_limit_usd: float
    approval_mode: str
    hash: str
    kill_switch_path: str | None = None

    def is_expired(self, now: datetime) -> bool:
        return now.date() > self.expires


def _require(d: dict[str, Any], key: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise MandateError(f"mandate missing required field: {key}")
    return d[key]


def parse_mandate(raw: dict[str, Any]) -> Mandate:
    symbols = raw.get("symbols") or {}
    allowed = symbols.get("allowed") or []
    if not isinstance(allowed, list) or not allowed:
        raise MandateError("mandate symbols.allowed must be a non-empty list")
    shorts = bool(symbols.get("shorts", False))

    try:
        created = date.fromisoformat(str(_require(raw, "created")))
        expires = date.fromisoformat(str(_require(raw, "expires")))
    except (TypeError, ValueError) as exc:
        raise MandateError(f"mandate dates invalid: {exc}") from exc
    if expires <= created:
        raise MandateError("mandate expires must be after created")

    canonical = {k: v for k, v in raw.items() if k != "hash"}
    m = Mandate(
        id=str(_require(raw, "id")),
        owner=str(_require(raw, "owner")),
        created=created,
        expires=expires,
        allowed=frozenset(str(s).upper() for s in allowed),
        shorts=shorts,
        max_notional_per_order_usd=float(_require(raw, "max_notional_per_order_usd")),
        max_total_exposure_usd=float(_require(raw, "max_total_exposure_usd")),
        min_cash_reserve_usd=float(raw.get("min_cash_reserve_usd") or 0.0),
        max_daily_trades=int(raw.get("max_daily_trades") or 0),
        daily_loss_limit_usd=float(raw.get("daily_loss_limit_usd") or 0.0),
        approval_mode=str(raw.get("approval_mode") or "manual"),
        hash=sha256_of(canonical),
        kill_switch_path=raw.get("kill_switch_path"),
    )
    declared = raw.get("hash")
    if declared and declared != m.hash:
        raise MandateError("mandate hash mismatch: mandate file was edited without re-signing")
    return m


def load_mandate(path: str | Path) -> Mandate:
    p = Path(path)
    if not p.exists():
        raise MandateError(f"mandate file not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise MandateError(f"cannot read mandate {p}: {exc}") from exc
    return parse_mandate(raw)


def write_mandate(path: str | Path, raw: dict[str, Any]) -> Mandate:
    """Write a re-signed mandate (hash recomputed); archive the old one first."""
    p = Path(path)
    old_hash = None
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            old_hash = old.get("hash")
        except (json.JSONDecodeError, OSError):
            old_hash = None
    doc = {k: v for k, v in raw.items() if k != "hash"}
    doc["hash"] = sha256_of(doc)
    p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    if old_hash and old_hash != doc["hash"]:
        archive = p.with_name(p.name + ".archive.jsonl")
        with archive.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                 "old_hash": old_hash, "new_hash": doc["hash"]}) + "\n")
    return parse_mandate(doc)
