"""The house risk gate (plan §4.2, design §4): named checks, one verdict.

The gate is the **only** component that may emit `ALLOW` (design D3). It
evaluates every check, reports all failures, and names the **first** failure in
:data:`signald.contracts.GATE_PRECEDENCE` as the binding gate - so a refusal is
never vague.

Three rules make it trustworthy:

* **Fail closed.** A check that cannot evaluate (missing ADV, missing stop,
  unknown data vintage) blocks. An exception inside a check is `BLOCK(<check>)`
  with `check_error` in the reason - never a pass, and never a crash for the
  caller.
* **Attributable reduction.** Only ``vol_regime``, ``house_cvar``,
  ``concentration``, ``liquidity`` and ``sleeve_capital`` may *reduce* rather
  than block (plan §4.2). Any other check passes or blocks, so a size that
  shrinks without a named reducer is a bug.
* **One computation.** The gate *composes* the risk engines (``tail``,
  ``voltarget``, ``ladder``) and the sizer then applies the result; when the
  gate returns `adjusted_risk_pct`, the caller passes ``vol_scalar=1.0`` to the
  sizer so the volatility adjustment is applied exactly once.

Research risk context (``research_decision.json``) is **advisory only**: it can
never move a verdict, and it is never read as a number (design §4.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import Config, parse_hhmm
from ..contracts import GATE_PRECEDENCE
from ..mandate import Mandate
from .ladder import LadderState
from .state import INTRADAY, BookState, MarketState, RiskRequest
from .tail import ESResult
from .voltarget import VolScalar

#: Checks allowed to shrink an order instead of refusing it (plan §4.2).
REDUCING_CHECKS = ("vol_regime", "house_cvar", "concentration", "liquidity", "sleeve_capital")

#: Setups whose thesis is mean reversion - the knife guard's target set.
REVERSION_SETUPS = ("VWAP_PULLBACK", "VWAP_REVERT", "GAP_FADE")
#: Setups that need a trend/range regime to be legal (design §6.2).
TREND_SETUPS = ("ORB_RVOL", "GAP_GO", "VWAP_PULLBACK")

#: Halt-reopen cooldown (plan §5.3: no entries for 5 minutes after a reopen).
REOPEN_COOLDOWN_S = 300.0

#: Producer invalidation syntax: "price_stop_loss: breach below 429.0".
_STOP_BREACH_RE = re.compile(
    r"price_stop_loss:\s*breach\s*(?:below|above)?\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GateContext:
    """Everything a check may read. Pure data - the gate performs no I/O.

    ``stage`` selects the consequence of a *soft* failure (missing/partial data,
    a closed market): ``"signal"`` downgrades and the signal is still recorded
    (Phase-A behaviour, kept for `signald/gates.py`), ``"order"`` refuses to
    submit. Hard failures block in both stages.
    """

    request: RiskRequest
    book: BookState
    market: MarketState
    mandate: Mandate
    config: Config
    now: datetime
    ladder: LadderState
    vol: VolScalar
    es: ESResult
    sleeve_ceiling_pct: float
    sleeve_deployed_pct: float
    setup_validated: bool = False
    day_type: str | None = None
    time_gate_ok: bool | None = None
    time_gate_reason: str | None = None
    other_sleeve_side: str | None = None
    wash_nets: bool = False
    research_risk_context: dict[str, Any] = field(default_factory=dict)
    # --- Phase-A (signal stage) inputs, folded in (plan §4, C7) ----------
    stage: str = "order"
    reference_complete: bool = True
    reference_stale: bool = False
    reference_ts: str | None = None
    market_open: bool | None = None
    positions_value_usd: float | None = None
    decision_age_days: int | None = None
    ingest_window_days: float = 1.0
    live_invalidations: tuple[str, ...] = ()
    last_signal: dict[str, Any] | None = None
    cooldown_hours: float = 12.0
    action: str | None = None


@dataclass(frozen=True)
class Failure:
    check: str
    kind: str  # "block" | "reduce"
    reason: str
    reason_code: str
    adjusted_risk_pct: float | None = None


@dataclass(frozen=True)
class GateDecision:
    verdict: str  # ALLOW | REDUCE | BLOCK  (== trade_permission)
    binding_gate: str | None
    reasons: tuple[str, ...]
    state_snapshot: dict[str, Any]
    decided_at: str
    config_hash: str
    adjusted_risk_pct: float | None = None
    approval_required: bool = False
    permission_reason_code: str = "ok"
    failures: tuple[Failure, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.verdict in {"ALLOW", "REDUCE"}


def projected_notional(req: RiskRequest) -> float:
    """Notional the request implies, for the pre-size cap checks.

    ``planned_notional_usd`` wins when the sleeve states one; otherwise
    ``equity * risk_pct * price / stop_distance`` - the same relation the sizer
    enforces, evaluated on the *uncapped* request, so it can only over-estimate
    (conservative for a cap check). The sizer's quantity stays authoritative.
    """
    if req.planned_notional_usd is not None:
        return max(0.0, float(req.planned_notional_usd))
    if req.stop_distance <= 0 or req.notional_per_share <= 0:
        return 0.0
    return req.equity * req.requested_risk_pct * req.notional_per_share / req.stop_distance


# --------------------------------------------------------------------------
# individual checks: return a Failure or None
# --------------------------------------------------------------------------
def _stage_kind(ctx: GateContext) -> str:
    """A soft failure downgrades a signal but blocks an order."""
    return "reduce" if ctx.stage == "signal" else "block"


def _mandate(ctx: GateContext) -> Failure | None:
    req, cfg, book = ctx.request, ctx.config, ctx.book
    if req.symbol not in ctx.mandate.allowed:
        return Failure("mandate", "block", f"{req.symbol} not in mandate allowed set", "symbol")
    if ctx.mandate.is_expired(ctx.now):
        return Failure("mandate", "block", "mandate expired", "expired")
    if req.implies_short() and not ctx.mandate.shorts:
        return Failure("mandate", "block", "short intent rejected: mandate shorts=false", "shorts")
    if ctx.mandate.max_daily_trades > 0 and book.sleeve_trades_today.get(req.sleeve, 0) >= (
        ctx.mandate.max_daily_trades
    ):
        traded = book.sleeve_trades_today.get(req.sleeve, 0)
        return Failure(
            "mandate", "block", f"daily cap reached ({traded})", "daily_cap"
        )
    if book.cash < float(ctx.mandate.min_cash_reserve_usd):
        return Failure(
            "mandate",
            "block",
            f"cash {book.cash:,.0f} < reserve {ctx.mandate.min_cash_reserve_usd:,.0f}",
            "cash_reserve",
        )
    if ctx.market.tradable is False:
        return Failure("mandate", "block", f"{req.symbol} not tradable", "not_tradable")

    # total exposure cap first: a book-level breach outranks one order's cap
    held = ctx.positions_value_usd
    if held is None:
        held = book.notional()
    projected = held + projected_notional(req)
    if projected > ctx.mandate.max_total_exposure_usd:
        room = ctx.mandate.max_total_exposure_usd - held
        adjusted = _risk_to_fit(ctx, room)
        if adjusted <= 0:
            return Failure(
                "mandate",
                "block",
                f"projected exposure {projected:,.0f} > cap "
                f"{ctx.mandate.max_total_exposure_usd:,.0f}",
                "exposure_cap",
            )
        return Failure(
            "mandate",
            "reduce",
            f"projected exposure {projected:,.0f} > cap "
            f"{ctx.mandate.max_total_exposure_usd:,.0f}",
            "exposure_cap",
            adjusted,
        )

    # per-order notional cap (Phase A downgraded it; shrinking is the right answer)
    if req.planned_notional_usd is not None and (
        req.planned_notional_usd > ctx.mandate.max_notional_per_order_usd
    ):
        adjusted = _risk_to_fit(ctx, ctx.mandate.max_notional_per_order_usd)
        if adjusted <= 0:
            return Failure(
                "mandate", "block", "order cap leaves no sizeable room", "order_cap"
            )
        return Failure(
            "mandate",
            "reduce",
            f"target notional {req.planned_notional_usd:,.0f} > cap "
            f"{ctx.mandate.max_notional_per_order_usd:,.0f}",
            "order_cap",
            adjusted,
        )

    if projected > cfg.leverage_cap * book.equity:
        return Failure(
            "mandate",
            "block",
            f"projected exposure {projected:,.0f} > leverage cap "
            f"{cfg.leverage_cap:g}x equity",
            "leverage_cap",
        )

    # cooldown: a repeat of the same action for the same name is a signal-stage
    # concern (the order path dedupes by client_order_id instead)
    if ctx.stage == "signal" and ctx.last_signal:
        cooldown = _cooldown_failure(ctx)
        if cooldown is not None:
            return cooldown
    return None


def _cooldown_failure(ctx: GateContext) -> Failure | None:
    last = ctx.last_signal or {}
    if last.get("ticker") != ctx.request.symbol:
        return None
    action = _action_of(ctx)
    if action is None or last.get("action") != action:
        return None
    stamp = _parse_iso(last.get("emitted_at"))
    if stamp is None:
        return None
    gap_h = max(0.0, (ctx.now - stamp).total_seconds() / 3600.0)
    if gap_h >= ctx.cooldown_hours:
        return None
    return Failure(
        "mandate",
        "reduce",
        f"cooldown: same {action} signal for {ctx.request.symbol} {gap_h:.1f}h ago",
        "cooldown",
        ctx.request.requested_risk_pct,
    )


def _sleeve_capital(ctx: GateContext) -> Failure | None:
    ceiling = ctx.sleeve_ceiling_pct * ctx.book.equity
    used = ctx.sleeve_deployed_pct * ctx.book.equity
    room = ceiling - used
    if room <= 0:
        return Failure(
            "sleeve_capital",
            "block",
            f"{ctx.request.sleeve} sleeve at its capital ceiling "
            f"({ctx.sleeve_deployed_pct:.2%} of {ctx.sleeve_ceiling_pct:.0%})",
            "sleeve_ceiling",
        )
    projected = projected_notional(ctx.request)
    if projected > room:
        adjusted = _risk_to_fit(ctx, room)
        if adjusted <= 0:
            return Failure(
                "sleeve_capital", "block", "no sleeve room for the requested risk", "no_room"
            )
        return Failure(
            "sleeve_capital",
            "reduce",
            f"projected sleeve notional {projected:,.0f} > room {room:,.0f}",
            "sleeve_ceiling",
            adjusted,
        )
    return None


def _house_drawdown(ctx: GateContext) -> Failure | None:
    ladder = ctx.ladder
    if ladder.halt:
        return Failure("house_drawdown", "block", ladder.reason, "halt")
    if ctx.request.sleeve == INTRADAY and not ladder.allow_new_intraday:
        return Failure("house_drawdown", "block", ladder.reason, "no_new_intraday")
    if ladder.size_multiplier < 1.0:
        adjusted = ctx.request.requested_risk_pct * ladder.size_multiplier
        return Failure(
            "house_drawdown",
            "reduce",
            f"{ladder.reason}: size x{ladder.size_multiplier:.2f}",
            ladder.reason,
            adjusted,
        )
    return None


def _house_cvar(ctx: GateContext) -> Failure | None:
    """Book ES plus this trade's full stop risk against the sleeve/house budget."""
    if ctx.stage != "order":
        # A signal publishes an expected-cost band, not a sized position: the
        # ES budget binds the order path (design §4.2 G3).
        return None
    if ctx.es.estimator == "unavailable" or ctx.es.window_days <= 0:
        # No usable estimate: a budget cannot be judged against a number that
        # does not exist (fail closed - this is the trade where the CVaR check
        # is most likely to be the thing that saves the book).
        return Failure(
            "house_cvar",
            "block",
            "no ES estimate available: CVaR budget cannot be judged",
            "es_unavailable",
        )
    budget = min(ctx.config.es_budget_house_pct, ctx.config.es_budget_sleeve_pct)
    projected = ctx.es.value_pct + ctx.request.requested_risk_pct
    if projected <= budget:
        return None
    adjusted = max(0.0, budget - ctx.es.value_pct)
    if adjusted <= 0:
        return Failure(
            "house_cvar",
            "block",
            f"book ES {ctx.es.value_pct:.2%} already over the {budget:.2%} budget"
            + (" (estimate flagged)" if ctx.es.flagged else ""),
            "es_budget",
        )
    return Failure(
        "house_cvar",
        "reduce",
        f"projected ES {projected:.2%} > budget {budget:.2%}",
        "es_budget",
        adjusted,
    )


def _correlation_stress(ctx: GateContext) -> Failure | None:
    if ctx.stage != "order":
        return None  # needs a quantity; the mandate's exposure cap covers a signal
    req, cfg, book = ctx.request, ctx.config, ctx.book
    key = req.cluster or req.sector or req.symbol
    projected = book.cluster_notional().get(key, 0.0) + projected_notional(req)
    cap = cfg.cluster_cap_pct * ctx.sleeve_ceiling_pct * book.equity
    if projected > cap:
        return Failure(
            "correlation_stress",
            "block",
            f"projected cluster {key} notional {projected:,.0f} > cap {cap:,.0f}",
            "cluster_cap",
        )
    return None


def _vol_regime(ctx: GateContext) -> Failure | None:
    scalar = ctx.vol.scalar
    if scalar >= 1.0 or not ctx.vol.warmup_ok:
        # warm-up unmet -> no scaling at all (never scale up on a thin estimate)
        return None
    adjusted = ctx.request.requested_risk_pct * max(0.0, scalar)
    if adjusted <= 0:
        return Failure(
            "vol_regime",
            "block",
            f"vol scalar {scalar:.2f} zeroes the requested risk",
            "vol_scalar_zero",
        )
    return Failure(
        "vol_regime",
        "reduce",
        f"realized vol {ctx.vol.realized:.1%} above target {ctx.vol.target:.1%} "
        f"-> size x{scalar:.2f}",
        "vol_above_target",
        adjusted,
    )


def _market_regime(ctx: GateContext) -> Failure | None:
    setup = (ctx.request.setup or "").upper()
    if setup not in TREND_SETUPS and setup not in REVERSION_SETUPS:
        return None  # a swing/research setup has no intraday regime requirement
    day_type = (ctx.day_type or "").lower()
    if day_type not in {"trend", "range"}:
        return Failure(
            "market_regime",
            "block",
            f"day type unknown ({ctx.day_type!r}): no intraday entries without a classifier",
            "day_type_unknown",
        )
    if setup in TREND_SETUPS and day_type != "trend":
        return Failure(
            "market_regime", "block", f"{setup} needs a trend day, got {day_type}", "setup_regime"
        )
    if setup in REVERSION_SETUPS and day_type != "range":
        return Failure(
            "market_regime", "block", f"{setup} needs a range day, got {day_type}", "setup_regime"
        )
    return None


def _knife_guard(ctx: GateContext) -> Failure | None:
    """Never buy into a falling knife: a mean-reversion entry against a shock."""
    setup = (ctx.request.setup or "").upper()
    if setup not in REVERSION_SETUPS:
        return None
    if ctx.market.news_shock:
        return Failure(
            "knife_guard", "block", "fresh news shock: no mean-reversion entry", "news_shock"
        )
    if (
        ctx.market.reopen_cooldown_s is not None
        and ctx.market.reopen_cooldown_s < REOPEN_COOLDOWN_S
    ):
        return Failure(
            "knife_guard",
            "block",
            f"within {REOPEN_COOLDOWN_S:.0f}s of a halt reopen",
            "reopen_cooldown",
        )
    impulse = ctx.market.impulse_pct
    atr_pct = _atr_pct(ctx.market)
    if impulse is not None and atr_pct is not None and impulse <= -2.0 * atr_pct:
        return Failure(
            "knife_guard",
            "block",
            f"adverse impulse {impulse:.2%} >= 2x ATR ({atr_pct:.2%}): falling knife",
            "adverse_impulse",
        )
    return None


def _concentration(ctx: GateContext) -> Failure | None:
    if ctx.stage != "order":
        return None  # position caps bind a sized order, not a recorded signal
    req, cfg, book = ctx.request, ctx.config, ctx.book
    cap_pct = (
        cfg.max_single_name_pct_intraday
        if req.sleeve == INTRADAY
        else cfg.max_single_name_pct_swing
    )
    existing = abs(book.position_for(req.symbol)) * req.notional_per_share
    projected = existing + projected_notional(req)
    cap = cap_pct * book.equity
    if projected > cap:
        room = cap - existing
        adjusted = _risk_to_fit(ctx, room)
        if adjusted <= 0:
            return Failure(
                "concentration",
                "block",
                f"{req.symbol} already at the {cap_pct:.0%} single-name cap",
                "single_name_cap",
            )
        return Failure(
            "concentration",
            "reduce",
            f"projected {req.symbol} {projected:,.0f} > cap {cap:,.0f}",
            "single_name_cap",
            adjusted,
        )
    max_positions = (
        cfg.max_positions_intraday if req.sleeve == INTRADAY else cfg.max_positions_swing
    )
    if book.count(req.sleeve) >= max_positions and book.position_for(req.symbol) == 0:
        return Failure(
            "concentration",
            "block",
            f"{req.sleeve} at max positions ({max_positions})",
            "max_positions",
        )
    if book.count() >= cfg.max_positions_swing + cfg.max_positions_intraday:
        return Failure("concentration", "block", "house at max total positions", "max_positions")
    if req.sleeve == INTRADAY:
        max_trades = cfg.intraday_max_trades_per_name
        if max_trades > 0 and book.trades_for(req.symbol) >= max_trades:
            return Failure(
                "concentration",
                "block",
                f"{req.symbol} already traded {max_trades}x today",
                "max_trades_per_name",
            )
    return None


def _liquidity(ctx: GateContext) -> Failure | None:
    req, cfg, market = ctx.request, ctx.config, ctx.market
    if ctx.stage != "order":
        # a signal has no quantity yet: participation is an order-time property.
        # The spread check still applies - it is a property of the tape itself.
        if market.spread_blown():
            return Failure(
                "liquidity",
                "block",
                f"spread {market.spread_bps:.1f}bps blown out vs median "
                f"{market.spread_median_bps:.1f}bps",
                "spread_blowout",
            )
        return None
    if req.notional_per_share <= 0 or market.last is None:
        return Failure("liquidity", "block", "no price: participation unknowable", "no_price")
    adv = market.adv_shares
    if adv is None or adv <= 0:
        return Failure(
            "liquidity", "block", "no ADV available: participation unknowable", "no_adv"
        )
    shares = projected_notional(req) / req.notional_per_share
    cap_shares = cfg.max_adv_pct * adv
    if shares > cap_shares:
        room_usd = cap_shares * req.notional_per_share
        adjusted = _risk_to_fit(ctx, room_usd)
        if adjusted <= 0:
            return Failure(
                "liquidity", "block", "participation cap leaves no sizeable room", "participation"
            )
        return Failure(
            "liquidity",
            "reduce",
            f"order {shares:,.0f} sh > {cfg.max_adv_pct:.0%} of ADV ({cap_shares:,.0f} sh)",
            "participation",
            adjusted,
        )
    if market.spread_blown():
        return Failure(
            "liquidity",
            "block",
            f"spread {market.spread_bps:.1f}bps blown out vs median "
            f"{market.spread_median_bps:.1f}bps",
            "spread_blowout",
        )
    return None


def _cost(ctx: GateContext) -> Failure | None:
    if ctx.stage != "order":
        return None  # a signal publishes an expected-cost band; no size to gate yet
    req, cfg = ctx.request, ctx.config
    move = req.measured_move_bps
    if move is None:
        return Failure("cost", "block", "expected move unknown: cost gate cannot run", "no_move")
    cost_bps = req.round_trip_cost_bps or _round_trip_cost_bps(ctx)
    if cost_bps <= 0:
        return Failure("cost", "block", "round-trip cost unknown", "no_cost")
    required = cfg.cost_gate_multiple * cost_bps
    if move <= required:
        return Failure(
            "cost",
            "block",
            f"expected move {move:.1f}bps <= {cfg.cost_gate_multiple:g}x round trip "
            f"({required:.1f}bps)",
            "cost_gate",
        )
    return None


def _wash(ctx: GateContext) -> Failure | None:
    """Self-cross with the other sleeve (broker answers 403, so we must not)."""
    if ctx.other_sleeve_side is None or ctx.other_sleeve_side == ctx.request.side:
        return None
    if ctx.wash_nets:
        return None  # netting reduces total exposure: legal
    return Failure(
        "wash",
        "block",
        f"{ctx.request.sleeve} {ctx.request.side} crosses the other sleeve's "
        f"{ctx.other_sleeve_side} in {ctx.request.symbol}",
        "self_cross",
    )


def _shortability(ctx: GateContext) -> Failure | None:
    req, market = ctx.request, ctx.market
    if not req.implies_short():
        return None
    if ctx.mandate.shorts is False:
        return Failure("shortability", "block", "shorts disabled by mandate", "shorts_off")
    if not market.shortable:
        return Failure(
            "shortability", "block", f"{req.symbol} is not easy-to-borrow", "not_etb"
        )
    if market.ssr:
        return Failure(
            "shortability",
            "block",
            f"{req.symbol} is in a short-sale restriction (SSR)",
            "ssr",
        )
    return None


def _data(ctx: GateContext) -> Failure | None:
    req, cfg, market = ctx.request, ctx.config, ctx.market
    if not ctx.reference_complete:
        return Failure(
            "data",
            "block",
            "reference data incomplete (cash/asset state unavailable) — fail closed",
            "reference_incomplete",
        )
    quality = (market.data_quality or "unknown").lower()
    if quality == "partial":
        return Failure(
            "data",
            _stage_kind(ctx),
            "data_quality=partial — treat decision as weaker",
            "data_quality_partial",
            ctx.request.requested_risk_pct,
        )
    if quality != "fresh":
        return Failure(
            "data", "block", f"data_quality={quality} (fresh required)", "data_quality"
        )
    if ctx.reference_stale:
        return Failure(
            "data",
            _stage_kind(ctx),
            f"reference quote stale (ts={ctx.reference_ts}) — treat price fields as "
            "questionable",
            "reference_stale",
            ctx.request.requested_risk_pct,
        )
    if market.quote_age_s is not None and market.quote_age_s > cfg.max_quote_staleness_s:
        return Failure(
            "data",
            "block",
            f"quote {market.quote_age_s:.1f}s old > {cfg.max_quote_staleness_s:g}s",
            "stale_quote",
        )
    if market.bar_age_s is not None and market.bar_age_s > cfg.max_bar_staleness_s:
        return Failure(
            "data",
            "block",
            f"bar {market.bar_age_s:.1f}s old > {cfg.max_bar_staleness_s:g}s",
            "stale_bar",
        )
    caliber = (market.price_caliber or "").strip().lower()
    if caliber in {"", "unknown", "mixed", "unresolved", "n/a", "none"}:
        return Failure(
            "data",
            _stage_kind(ctx),
            f"price_caliber={caliber or 'unset'} — price sanity not computed",
            "price_caliber",
            ctx.request.requested_risk_pct,
        )
    if ctx.decision_age_days is not None and ctx.decision_age_days > ctx.ingest_window_days:
        return Failure(
            "data",
            "block",
            f"decision older than ingest window ({ctx.ingest_window_days:g}d)",
            "ingest_window",
        )
    breach = _invalidation_failure(ctx)
    if breach is not None:
        return breach
    if req.stop_price is None:
        return Failure(
            "data",
            _stage_kind(ctx),
            "no protective stop: a stop must exist with the entry",
            "no_stop",
            ctx.request.requested_risk_pct,
        )
    return None


def _invalidation_failure(ctx: GateContext) -> Failure | None:
    """A producer-declared invalidation: a live breach suppresses the trade."""
    for inv in ctx.live_invalidations:
        match = _STOP_BREACH_RE.search(inv)
        price = ctx.market.last
        if match and price is not None:
            level = _float_or_inf(match.group(1))
            if price <= level:
                return Failure("data", "block", f"invalidation live: {inv}", "invalidation")
            continue
        if ctx.stage == "signal":
            return Failure(
                "data",
                "reduce",
                f"invalidation present: {inv}",
                "invalidation_advisory",
                ctx.request.requested_risk_pct,
            )
    return None


def _float_or_inf(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return float("inf")


def _action_of(ctx: GateContext) -> str | None:
    return (ctx.action or "").strip().upper() or None


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _time(ctx: GateContext) -> Failure | None:
    req, cfg, now = ctx.request, ctx.config, ctx.now
    if ctx.market.halted:
        return Failure(
            "halt",
            "block",
            f"instrument halted ({ctx.market.halt_reason or 'halt state'})",
            "halted",
        )
    if ctx.market.session in {None, "closed"}:
        if ctx.stage == "signal":
            return Failure(
                "time",
                "reduce",
                "market closed — next-session signal",
                "market_closed",
                req.requested_risk_pct,
            )
        return Failure("time", "block", f"session={ctx.market.session!r}: no entries", "session")
    if req.sleeve != INTRADAY:
        return None
    at = now.time()
    after = parse_hhmm(cfg.intraday_entry_after)
    before = parse_hhmm(cfg.intraday_entry_before)
    flat_by = parse_hhmm(cfg.intraday_flat_by)
    if at < after:
        return Failure(
            "time",
            "block",
            f"before the {cfg.intraday_entry_after} entry window",
            "too_early",
        )
    if at > before:
        if at > flat_by:
            return Failure(
                "time", "block", f"after flat-by {cfg.intraday_flat_by}", "after_flat_by"
            )
        return Failure(
            "time", "block", f"after the {cfg.intraday_entry_before} cutoff", "too_late"
        )
    if ctx.time_gate_ok is False:
        return Failure(
            "time", "block", ctx.time_gate_reason or "time gate closed", "time_gate"
        )
    return None


def _approval(ctx: GateContext) -> Failure | None:
    """Approval is not a permission by itself: it holds the order as REDUCE."""
    notional = projected_notional(ctx.request)
    mode = (ctx.mandate.approval_mode or "manual").lower()
    threshold = ctx.mandate.max_notional_per_order_usd * ctx.config.approval_threshold_factor
    if mode in {"manual-high-order", "approval"} and notional >= threshold:
        return Failure(
            "approval",
            "reduce",
            f"notional {notional:,.0f} >= approval threshold {threshold:,.0f}: "
            "operator approval required",
            "approval_required",
            ctx.request.requested_risk_pct,
        )
    return None


#: Precedence order = the check order (contracts.GATE_PRECEDENCE).
CHECKS: dict[str, Any] = {
    "mandate": _mandate,
    "sleeve_capital": _sleeve_capital,
    "house_drawdown": _house_drawdown,
    "house_cvar": _house_cvar,
    "correlation_stress": _correlation_stress,
    "vol_regime": _vol_regime,
    "market_regime": _market_regime,
    "knife_guard": _knife_guard,
    "concentration": _concentration,
    "liquidity": _liquidity,
    "cost": _cost,
    "wash": _wash,
    "shortability": _shortability,
    "data": _data,
    "time": _time,
    "approval": _approval,
}


def evaluate(ctx: GateContext) -> GateDecision:
    """Run every check; return the verdict, the binding gate and all reasons."""
    failures: dict[str, Failure] = {}
    for name in GATE_PRECEDENCE:
        check = CHECKS.get(name)
        if check is None:
            continue  # a precedence name with no check is a vocabulary placeholder
        try:
            failure = check(ctx)
        except Exception as exc:  # noqa: BLE001 - a check that raises must block, never pass
            failure = Failure(name, "block", f"check_error: {exc!r}", "check_error")
        if failure is not None:
            failures[name] = failure

    ordered = [failures[name] for name in GATE_PRECEDENCE if name in failures]
    blocks = [f for f in ordered if f.kind == "block"]
    reduces = [f for f in ordered if f.kind == "reduce"]

    if blocks:
        verdict, binding = "BLOCK", blocks[0]
    elif reduces:
        verdict, binding = "REDUCE", reduces[0]
    else:
        verdict, binding = "ALLOW", None

    adjusted: float | None = None
    if verdict == "REDUCE":
        adjusted = min(
            f.adjusted_risk_pct for f in reduces if f.adjusted_risk_pct is not None
        )
    approval_required = any(f.check == "approval" for f in ordered)
    reason_code = binding.reason_code if binding is not None else "ok"

    snapshot = {
        "equity": round(ctx.book.equity, 2),
        "cash": round(ctx.book.cash, 2),
        "heat_pct": round(ctx.book.heat_pct, 6),
        "es_975_1d_pct": round(ctx.es.value_pct, 6),
        "es_estimator": ctx.es.estimator,
        "es_flagged": ctx.es.flagged,
        "drawdown_pct": round(ctx.book.drawdown_pct, 6),
        "day_pnl_pct": round(ctx.book.day_pnl_pct, 6),
        "ladder_rung": ctx.ladder.rung,
        "vol_scalar": round(ctx.vol.scalar, 4),
        "vol_applied": ctx.vol.applied,
        "sleeve": ctx.request.sleeve,
        "sleeve_deployed_pct": round(ctx.sleeve_deployed_pct, 6),
        "sleeve_ceiling_pct": round(ctx.sleeve_ceiling_pct, 6),
        "projected_notional_usd": round(projected_notional(ctx.request), 2),
        "advisory_research_regime": ctx.research_risk_context.get("regime"),
    }

    return GateDecision(
        verdict=verdict,
        binding_gate=binding.check if binding is not None else None,
        reasons=tuple(f"{f.kind.upper()} {f.check}: {f.reason}" for f in ordered)
        or ("all checks passed",),
        state_snapshot=snapshot,
        decided_at=ctx.now.isoformat(timespec="seconds"),
        config_hash=ctx.config.config_hash(),
        adjusted_risk_pct=adjusted,
        approval_required=approval_required,
        permission_reason_code=reason_code,
        failures=tuple(ordered),
    )


# --------------------------------------------------------------------------
def _risk_to_fit(ctx: GateContext, room_usd: float) -> float:
    """Risk fraction whose projected notional fits ``room_usd`` (0 when impossible)."""
    if room_usd <= 0:
        return 0.0
    notional_per_risk = projected_notional_per_risk(ctx.request)
    if notional_per_risk <= 0:
        return 0.0
    return max(0.0, min(ctx.request.requested_risk_pct, room_usd / notional_per_risk))


def projected_notional_per_risk(req: RiskRequest) -> float:
    """Notional per unit of risk fraction (the projection's slope)."""
    if req.stop_distance <= 0 or req.notional_per_share <= 0:
        return 0.0
    return req.equity * req.notional_per_share / req.stop_distance


def _atr_pct(market: MarketState) -> float | None:
    if market.atr is None or market.last is None or market.last <= 0:
        return None
    return market.atr / market.last


def _round_trip_cost_bps(ctx: GateContext) -> float:
    """Default round-trip cost: low-float names pay the wider band (plan §5.2)."""
    adv = ctx.market.adv_shares
    if adv is not None and adv < ctx.config.min_adv_shares:
        return ctx.config.cost_lowfloat_bps
    return ctx.config.cost_liquid_bps
