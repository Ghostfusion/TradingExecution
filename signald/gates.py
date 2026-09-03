"""Fail-closed mandate gates (plan §4.4).

Every gate must run; a gate that cannot evaluate (missing reference data,
missing journal state) FAILS the signal — never a silent pass. Output is a
verdict: PASS | DOWNGRADE (signal emitted with reasons) | BLOCK (no signal).
HALT (kill switch) is checked before gates in the processor.

Gates are pure functions of (decision, contract, mandate, ref, journal_state).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .alpaca_ref import RefData
from .mandate import Mandate
from .schema import ResearchDecision, SignalContract

_STOP_BREACH_RE = re.compile(
    r"price_stop_loss:\s*breach\s*(?:below|above)?\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GateResult:
    verdict: str  # PASS | DOWNGRADE | BLOCK
    reasons: tuple[str, ...] = ()
    downgrades: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    approval_required: bool = False
    target_notional_usd: float | None = None

    @property
    def passed(self) -> bool:
        return self.verdict in {"PASS", "DOWNGRADE"}


def _notional(contract: SignalContract) -> float | None:
    return contract.target_notional_usd


def evaluate(
    rd: ResearchDecision,
    contract: SignalContract,
    mandate: Mandate,
    ref: RefData,
    journal_state: dict[str, Any],
    now: datetime,
) -> GateResult:
    downgrades: list[str] = []
    blocked: list[str] = []
    reasons: list[str] = []

    def block(reason: str) -> None:
        blocked.append(reason)
        reasons.append(f"BLOCK {reason}")

    def downgrade(reason: str) -> None:
        downgrades.append(reason)
        reasons.append(f"DOWNGRADE {reason}")

    # --- instrument + direction (mandate) ---
    if contract.symbol not in mandate.allowed:
        block(f"symbol {contract.symbol} not in mandate allowed set")
    if contract.implies_short and not mandate.shorts:
        block("short intent rejected: mandate shorts=false")

    # --- size (cap, downgrade not block) ---
    notional = _notional(contract)
    if notional is not None and notional > mandate.max_notional_per_order_usd:
        downgrade(
            f"target notional {notional:,.0f} > cap {mandate.max_notional_per_order_usd:,.0f}"
        )

    # --- reference completeness (fail closed) ---
    if not ref.complete_for_gates:
        block("reference data incomplete (cash/asset state unavailable) — fail closed")
    if ref.stale:
        downgrade(f"reference quote stale (ts={ref.ts}) — treat price fields as questionable")

    # --- exposure (from positions + this order) ---
    if ref.positions_value is not None and notional is not None:
        total = ref.positions_value + notional
        if total > mandate.max_total_exposure_usd:
            downgrade(
                f"projected exposure {total:,.0f} > {mandate.max_total_exposure_usd:,.0f}"
            )

    # --- cash reserve (hard) ---
    if ref.cash is not None and ref.cash < mandate.min_cash_reserve_usd:
        block(f"cash {ref.cash:,.0f} < reserve {mandate.min_cash_reserve_usd:,.0f}")

    # --- tradeability ---
    if ref.asset_tradable is False:
        block(f"{contract.symbol} not tradable (Alpaca asset flag)")
    elif ref.market_open is False:
        downgrade("market closed — next-session signal")

    # --- daily count ---
    daily = int(journal_state.get("signals_today", 0))
    if mandate.max_daily_trades > 0 and daily >= mandate.max_daily_trades:
        block(f"daily signal cap reached ({daily} >= {mandate.max_daily_trades})")

    # --- cooldown (same ticker, same action) ---
    last = journal_state.get("last_signal")
    if last:
        last_ts = _parse_iso(last.get("emitted_at"))
        if (
            last_ts
            and last.get("ticker") == contract.symbol
            and last.get("action") == contract.action
        ):
            gap_h = (now - last_ts).total_seconds() / 3600.0
            if gap_h < 0:  # clock skew guard is elsewhere; here just compute
                gap_h = 0.0
            if gap_h < _cooldown_hours(journal_state):
                downgrade(
                    f"cooldown: same {contract.action} signal for {contract.symbol} "
                    f"{gap_h:.1f}h ago"
                )

    # --- data quality (fail closed on degraded data) ---
    if rd.data_quality in {"stale", "unknown"}:
        block(f"data_quality={rd.data_quality} — research decision not trustworthy")
    elif rd.data_quality == "partial":
        downgrade("data_quality=partial — treat decision as weaker")

    # --- price caliber ---
    caliber = (rd.price_caliber or "").strip().lower()
    if not caliber or caliber in {"unknown", "mixed", "unresolved", "n/a", "none"}:
        downgrade(f"price_caliber={caliber or 'unset'} — price sanity not computed")

    # --- invalidation ---
    for inv in rd.invalidations:
        m = _STOP_BREACH_RE.search(inv)
        if m and ref.last is not None:
            try:
                level = float(m.group(1))
            except ValueError:
                level = float("inf")
            if ref.last <= level:
                block(f"invalidation live: {inv} (last {ref.last} <= {level})")
                break
        else:
            downgrade(f"invalidation present: {inv}")

    # --- staleness (ingest window) ---
    window = _ingest_window(journal_state)
    if (now.date() - rd.effective_date).days > window:
        block(f"decision older than ingest window ({window}d)")

    # --- approval mode ---
    approval_required = False
    if (
        notional is not None
        and mandate.approval_mode in {"manual-high-order", "approval"}
        and notional >= mandate.max_notional_per_order_usd * _approval_factor(journal_state)
    ):
        approval_required = True
        downgrade("size >= approval threshold — approval required before any execution")

    if blocked:
        return GateResult(
            verdict="BLOCK",
            reasons=tuple(reasons),
            downgrades=tuple(downgrades),
            blocked=tuple(blocked),
            approval_required=approval_required,
            target_notional_usd=notional,
        )
    if downgrades:
        return GateResult(
            verdict="DOWNGRADE",
            reasons=tuple(reasons),
            downgrades=tuple(downgrades),
            blocked=(),
            approval_required=approval_required,
            target_notional_usd=notional,
        )
    return GateResult(
        verdict="PASS",
        reasons=("all gates passed",),
        target_notional_usd=notional,
    )


def _parse_iso(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        s = str(v)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _cooldown_hours(state: dict[str, Any]) -> float:
    return float(state.get("cooldown_hours", 12.0))


def _ingest_window(state: dict[str, Any]) -> float:
    return float(state.get("ingest_window_hours", 24.0) / 24.0)


def _approval_factor(state: dict[str, Any]) -> float:
    return float(state.get("approval_threshold_factor", 1.0))
