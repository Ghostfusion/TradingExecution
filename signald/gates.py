"""Signal-stage facade over the house risk gate (plan §4, C7).

Phase A judged a *signal*: PASS / DOWNGRADE (emit with reasons) / BLOCK. That
verdict is now produced by one implementation, :mod:`signald.risk.gate`, and
this module only translates the signal-stage inputs (`ResearchDecision`,
`SignalContract`, `RefData`, journal state) into a `GateContext` and the
decision back into a `GateResult`. It owns no rule of its own - if a rule is
not in `risk/gate.py`, it does not exist.

The two stages differ in exactly two places, both documented in the gate:

* a **soft** failure (missing/partial data, a closed market, a missing stop)
  downgrades a signal instead of refusing it - a signal is a recorded
  recommendation for the next session, an order is not;
* the checks that need a **quantity or a measured move** (participation,
  cluster stress, cost gate, concentration) are order-stage only: a signal has
  no size yet, and its notional exposure is capped by the mandate here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .alpaca_ref import RefData
from .config import Config
from .contracts import GATE_PRECEDENCE
from .mandate import Mandate
from .risk.gate import GateContext, GateDecision
from .risk.gate import evaluate as evaluate_house_gate
from .risk.ladder import LadderState
from .risk.state import SWING, BookState, MarketState, RiskRequest
from .risk.tail import ESResult
from .risk.voltarget import VolScalar
from .schema import ResearchDecision, SignalContract

#: Phase A's verdict vocabulary, kept for existing consumers (envelope shape).
_VERDICT = {"ALLOW": "PASS", "REDUCE": "DOWNGRADE", "BLOCK": "BLOCK"}


@dataclass(frozen=True)
class GateResult:
    verdict: str  # PASS | DOWNGRADE | BLOCK
    reasons: tuple[str, ...] = ()
    downgrades: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    approval_required: bool = False
    target_notional_usd: float | None = None
    #: The v2 fields (additive): the gate's own vocabulary and binding check.
    trade_permission: str = "ALLOW"
    binding_gate: str | None = None
    adjusted_risk_pct: float | None = None
    permission_reason_code: str = "ok"
    state_snapshot: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return self.verdict in {"PASS", "DOWNGRADE"}


def _spread_bps(ref: RefData) -> float | None:
    if ref.spread_usd is None or not ref.last:
        return None
    return (ref.spread_usd / 2.0) / ref.last * 1e4


def _quote_age_s(ref: RefData, now: datetime) -> float | None:
    """Age of the reference quote, in seconds.

    Both sides are normalised to UTC-aware first: a naive stamp follows the
    envelope convention (UTC), and an aware one is converted - so neither a
    tz-free daemon clock nor an aware injected clock changes the answer.
    """
    if ref.ts is None:
        return None
    stamp = ref.ts.replace(tzinfo=UTC) if ref.ts.tzinfo is None else ref.ts.astimezone(UTC)
    current = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return max(0.0, (current - stamp).total_seconds())


def _request(contract: SignalContract, ref: RefData, sleeve: str = SWING) -> RiskRequest:
    stop = contract.stop_price
    stop_distance = 0.0
    if stop is not None and ref.last is not None:
        stop_distance = abs(ref.last - stop)
    side = "sell" if contract.action in {"REDUCE", "EXIT"} else "buy"
    return RiskRequest(
        sleeve=sleeve,
        symbol=contract.symbol,
        setup=str(contract.strategy or "VALUE_DIP"),
        side=side,
        stop_distance=stop_distance,
        price=float(ref.last or 0.0),
        equity=float(ref.equity or 0.0),
        requested_risk_pct=float(contract.target_pct or 0.0),
        stop_price=stop,
        target_price=contract.target_price,
        planned_notional_usd=contract.target_notional_usd,
        intent_id=contract.decision_hash,
    )


def build_context(
    rd: ResearchDecision,
    contract: SignalContract,
    mandate: Mandate,
    ref: RefData,
    journal_state: dict[str, Any],
    now: datetime,
    config: Config,
) -> GateContext:
    """Translate the Phase-A inputs into the gate's typed context."""
    request = _request(contract, ref)
    equity = float(ref.equity or 0.0)
    cash = float(ref.cash or 0.0)
    ingest_hours = float(journal_state.get("ingest_window_hours", 24.0) or 24.0)
    signals_today = int(journal_state.get("signals_today", 0) or 0)
    book = BookState(
        equity=equity,
        cash=cash,
        heat_pct=0.0,
        es_pct=0.0,
        sleeve_trades_today={request.sleeve: signals_today},
        trades_today={},
    )
    market = MarketState(
        symbol=contract.symbol,
        last=ref.last,
        spread_bps=_spread_bps(ref),
        spread_median_bps=None,
        quote_age_s=_quote_age_s(ref, now),
        bar_age_s=None,
        feed=str(ref.feed or "sip"),
        as_of=ref.ts,
        session="rth" if ref.market_open is not False else "closed",
        tradable=ref.asset_tradable if ref.asset_tradable is not None else True,
        shortable=False,
        adv_shares=None,
        atr=None,
        data_quality=rd.data_quality,
        price_caliber=rd.price_caliber,
    )
    return GateContext(
        request=request,
        book=book,
        market=market,
        mandate=mandate,
        config=config,
        now=now,
        ladder=LadderState(
            rung=0,
            size_multiplier=1.0,
            allow_new_intraday=True,
            flatten_intraday=False,
            freeze_allocation=False,
            halt=False,
            reason="none",
        ),
        vol=VolScalar(
            scalar=1.0,
            target=0.10,
            realized=0.10,
            warmup_ok=False,
            applied=False,
            reason="warmup",
        ),
        es=ESResult(
            value_pct=0.0,
            estimator="unavailable",
            flagged=True,
            window_days=0,
            components={},
        ),
        sleeve_ceiling_pct=1.0,
        sleeve_deployed_pct=0.0,
        stage="signal",
        reference_complete=ref.complete_for_gates,
        reference_stale=bool(ref.stale),
        reference_ts=None if ref.ts is None else str(ref.ts),
        market_open=ref.market_open,
        positions_value_usd=ref.positions_value,
        decision_age_days=(now.date() - rd.effective_date).days,
        ingest_window_days=ingest_hours / 24.0,
        live_invalidations=tuple(rd.invalidations),
        last_signal=journal_state.get("last_signal"),
        cooldown_hours=float(journal_state.get("cooldown_hours", 12.0) or 12.0),
        action=contract.action,
        research_risk_context=dict(rd.risk_context),
        held_symbols=ref.held_symbols,
    )


def evaluate(
    rd: ResearchDecision,
    contract: SignalContract,
    mandate: Mandate,
    ref: RefData,
    journal_state: dict[str, Any],
    now: datetime,
    config: Config | None = None,
) -> GateResult:
    """Phase-A verdict from the single producer (see module docstring)."""
    cfg = config if config is not None else Config()
    ctx = build_context(rd, contract, mandate, ref, journal_state, now, cfg)
    return to_gate_result(evaluate_house_gate(ctx), contract)


def to_gate_result(decision: GateDecision, contract: SignalContract) -> GateResult:
    blocked = tuple(f.reason for f in decision.failures if f.kind == "block")
    downgrades = tuple(f.reason for f in decision.failures if f.kind == "reduce")
    reasons = tuple(
        f"{'BLOCK' if f.kind == 'block' else 'DOWNGRADE'} {f.reason}" for f in decision.failures
    ) or ("all gates passed",)
    return GateResult(
        verdict=_VERDICT[decision.verdict],
        reasons=reasons,
        downgrades=downgrades,
        blocked=blocked,
        approval_required=decision.approval_required,
        target_notional_usd=contract.target_notional_usd,
        trade_permission=decision.verdict,
        binding_gate=decision.binding_gate,
        adjusted_risk_pct=decision.adjusted_risk_pct,
        permission_reason_code=decision.permission_reason_code,
        state_snapshot=decision.state_snapshot,
    )


__all__ = [
    "GATE_PRECEDENCE",
    "GateResult",
    "build_context",
    "evaluate",
    "to_gate_result",
]
