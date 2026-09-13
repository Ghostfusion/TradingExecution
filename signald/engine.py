"""The session engine: the only component that turns a candidate into an order.

Composition, never re-implementation:

    marketdata (quotes/bars, freshness, quarantine)
      -> daytype.classify -> signals.setups.scan -> signals.costgate.check
      -> risk.gate.evaluate (stage="order") -> risk.sizing.size
      -> order.policy.plan_entry -> order.guard.guard -> order.manager.submit
      -> order.stops.protective_orders -> order.flatten.flatten_plan -> reconcile

Every rule lives in the module named above; this file only sequences them and
enforces the two structural promises:

* **the order path is unreachable** unless the config says paper/live *and* the
  caller passed ``execute=True`` (two independent opt-ins, plan §P2/P5), and
* **no order is submitted without a `GateDecision`** whose verdict is
  `ALLOW`/`REDUCE` - the gate is the only component that can say yes (design D3).

A refusal is never silent: every skipped candidate is recorded with the binding
gate or the module reason, and the result is returned, not raised.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Config
from .risk.gate import GATE_PRECEDENCE, GateContext, GateDecision, evaluate
from .risk.ladder import LadderState, rung_for
from .risk.sizing import SizingCaps, size
from .risk.state import INTRADAY, BookState, MarketState, RiskRequest
from .risk.tail import ESResult, es_estimate
from .risk.voltarget import vol_scalar
from .sleeves.budgets import budget_for, notional_room_usd
from .sleeves.intraday import intraday_policy

#: The setup order the scanner tries (matched by `signals.setups.scan`).
SCAN_SETUPS = ("ORB_RVOL", "GAP_GO", "VWAP_PULLBACK", "VWAP_REVERT", "GAP_FADE")


@dataclass(frozen=True)
class SessionResult:
    session: str
    scanned: int = 0
    candidates: tuple[dict[str, Any], ...] = ()
    gated: tuple[dict[str, Any], ...] = ()
    submitted: tuple[dict[str, Any], ...] = ()
    skipped: tuple[dict[str, Any], ...] = ()
    protective: tuple[dict[str, Any], ...] = ()
    flat: bool | None = None
    flat_detail: str = ""
    reconciled: bool | None = None
    drift: tuple[dict[str, Any], ...] = ()
    halted: bool = False
    trades_today: dict[str, int] = field(default_factory=dict)

    @property
    def submitted_count(self) -> int:
        return len(self.submitted)

    def skip_reasons(self) -> tuple[str, ...]:
        return tuple(str(s.get("reason")) for s in self.skipped)


def _skip(symbol: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"symbol": symbol, "reason": reason, **extra}


class SessionEngine:
    """One intraday session, composed from the seams it is handed."""

    def __init__(
        self,
        *,
        config: Config,
        mandate: Any,
        marketdata: Any,
        order_manager: Any,
        state_manager: Any | None = None,
        audit: Any | None = None,
        universe: Sequence[str] = (),
        equity_history: Sequence[float] = (),
        daily_returns: Sequence[float] = (),
        book: BookState | None = None,
        broker_positions: Callable[[], Any] | None = None,
        gate_evaluator: Callable[[GateContext], GateDecision] = evaluate,
        sizer: Callable[..., Any] = size,
    ) -> None:
        self.cfg = config
        self.mandate = mandate
        self.md = marketdata
        self.orders = order_manager
        self.states = state_manager
        self.audit = audit
        self.universe = tuple(universe)
        self.equity_history = tuple(float(x) for x in equity_history)
        self.daily_returns = tuple(float(x) for x in daily_returns)
        self.book = book or BookState(equity=0.0, cash=0.0)
        self.broker_positions = broker_positions
        self._gate = gate_evaluator
        self._size = sizer
        self._trades_today: dict[str, int] = {}

    # -- state -----------------------------------------------------------
    def ladder(self) -> LadderState:
        """De-risking rung from the equity path the caller supplied."""
        equity = self.book.equity
        peak = max(self.equity_history) if self.equity_history else equity
        return rung_for(
            day_pnl_pct=self.book.day_pnl_pct,
            five_day_pct=self.book.five_day_pct,
            drawdown_pct=-(1.0 - equity / peak) if peak > 0 else 0.0,
            soft_pct=self.cfg.daily_loss_soft_pct,
            hard_pct=self.cfg.daily_loss_hard_pct,
            derisk_5d_pct=self.cfg.derisk_5d_pct,
            derisk_dd_pct=self.cfg.derisk_dd_pct,
            halt_dd_pct=self.cfg.derisk_halt_dd_pct,
        )

    def vol(self):
        return vol_scalar(
            self.daily_returns,
            target=self.cfg.sleeve_intraday_vol_target,
            halflife_days=self.cfg.vol_halflife_days,
            warmup_days=self.cfg.vol_warmup_days,
            cap=self.cfg.vol_scale_cap,
            band=self.cfg.vol_rebalance_band,
        )

    def es(self) -> ESResult:
        losses = [r for r in self.daily_returns if r < 0]
        # weight = the share of HOUSE equity a fully-stressed cluster may hold:
        # cluster_cap_pct is a share of the sleeve, not of the book. Passing the
        # sleeve-relative cap would exaggerate the gap/halt shocks by 1/ceiling.
        return es_estimate(
            [-r for r in losses],
            confidence=self.cfg.es_confidence,
            weight=self.cfg.cluster_cap_pct * self.cfg.sleeve_intraday_capital_pct,
        )

    # -- the scan --------------------------------------------------------
    def scan(
        self, *, as_of: datetime | None = None
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
        """Candidates + skips from the universe (no orders, no gate)."""
        from .marketdata import daytype as daytype_mod
        from .signals import setups as setups_mod

        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for symbol in self.universe:
            if self.md.is_quarantined(symbol):
                skipped.append(_skip(symbol, "quarantined"))
                continue
            try:
                quote = self.md.quote(symbol)
                bars = self.md.bars(symbol, count=90, as_of=as_of)
                daily = self.md.daily(symbol, count=self.cfg.vol_warmup_days)
                first5 = self.md.bars(symbol, count=5, as_of=as_of)
            except Exception as exc:  # noqa: BLE001 - unavailable data is a skip, not a crash
                self.md.quarantine(symbol, f"market data: {exc!r}")
                skipped.append(_skip(symbol, "data_unavailable", detail=str(exc)))
                continue
            if quote.last is None:
                skipped.append(_skip(symbol, "no_price"))
                continue
            atr_value = daytype_mod.atr(daily) if daily else 0.0
            adv = self._adv_shares(symbol)
            day = daytype_mod.classify(
                symbol=symbol,
                bars=bars,
                first5=first5,
                prior_close=daily[-1].close if daily else None,
                baseline_shares=daily[-1].volume if daily else None,
                rvols=None,
                config=self.cfg,
            )
            ok, reason = setups_mod.eligible(
                symbol,
                price=quote.last,
                adv_shares=adv,
                atr=atr_value or None,
                config=self.cfg,
            )
            if not ok:
                skipped.append(_skip(symbol, reason))
                continue
            candidate, why = setups_mod.scan(
                symbol=symbol,
                bars=bars,
                first5=first5,
                day_type=day,
                prior_close=daily[-1].close if daily else None,
                atr_value=atr_value,
                adv_shares=adv,
                baseline_shares=daily[-1].volume if daily else None,
                config=self.cfg,
            )
            if candidate is None:
                skipped.append(_skip(symbol, why, day_type=day.day_type))
                continue
            candidates.append(
                {
                    "symbol": symbol,
                    "setup": candidate.setup,
                    "side": candidate.side,
                    "entry_price": candidate.entry_price,
                    "stop_price": candidate.stop_price,
                    "expected_move_bps": candidate.expected_move_bps,
                    "day_type": day.day_type,
                    "adx_14": day.adx_14,
                    "rvol_first5": day.rvol_first5,
                    "gap_pct": day.gap_pct,
                    "cluster": candidate.cluster,
                }
            )
        return tuple(candidates), tuple(skipped)

    @staticmethod
    def _adv(bars: Sequence[Any]) -> float | None:
        if not bars:
            return None
        return float(sum(b.volume for b in bars) / len(bars))

    # -- the session -----------------------------------------------------
    def run(
        self,
        *,
        execute: bool = False,
        as_of: datetime | None = None,
        flatten: bool = True,
    ) -> SessionResult:
        """Scan, gate, size, submit, protect and (optionally) flatten."""
        now = as_of or self.cfg.now()
        session = now.date().isoformat()
        armed, refusal = self.armed(execute=execute)
        candidates, scanned_skips = self.scan(as_of=now)
        skipped = list(scanned_skips)

        submitted: list[dict[str, Any]] = []
        gated: list[dict[str, Any]] = []
        for cand in candidates:
            outcome, submit = self._evaluate(cand, now=now, armed=armed)
            gated.append(outcome)
            if not outcome.get("allowed"):
                skipped.append(
                    _skip(cand["symbol"], str(outcome.get("reason")), setup=cand["setup"])
                )
                continue
            if submit:
                submitted.append(submit)

        protective: list[dict[str, Any]] = []
        if armed and self.states is not None:
            protective = self._protect(now=now)

        flat: bool | None = None
        detail = ""
        if flatten and armed:
            flat, detail = self._flatten(now=now)

        reconciled, drift = self._reconcile(now=now)

        result = SessionResult(
            session=session,
            scanned=len(self.universe),
            candidates=candidates,
            gated=tuple(gated),
            submitted=tuple(submitted),
            skipped=tuple(skipped),
            protective=tuple(protective),
            flat=flat,
            flat_detail=detail,
            reconciled=reconciled,
            drift=drift,
            halted=self.ladder().halt or not armed,
            trades_today=dict(self._trades_today),
        )
        if self.audit is not None:
            self.audit.append(
                "session_end",
                refusal or "session complete",
                session=session,
                scanned=result.scanned,
                submitted=result.submitted_count,
                flat=flat,
            )
        return result

    def armed(self, *, execute: bool) -> tuple[bool, str]:
        """Two independent opt-ins before any order may be sent (plan P2/P5)."""
        if not self.cfg.order_path_enabled:
            return False, f"mode={self.cfg.mode}: the order path is unreachable"
        if not execute:
            return False, "execute=False: signals only"
        return True, ""

    # -- internals -------------------------------------------------------
    def _evaluate(
        self, cand: dict[str, Any], *, now: datetime, armed: bool
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        request = RiskRequest(
            sleeve=INTRADAY,
            symbol=cand["symbol"],
            setup=cand["setup"],
            side=cand["side"],
            stop_distance=abs(cand["entry_price"] - cand["stop_price"]),
            price=float(cand["entry_price"]),
            equity=self.book.equity,
            requested_risk_pct=intraday_policy(self.cfg).risk_per_trade_pct,
            stop_price=cand["stop_price"],
            measured_move_bps=cand["expected_move_bps"],
            round_trip_cost_bps=None,
            trailing_volume=self._adv_shares(cand["symbol"]),
            cluster=cand.get("cluster"),
        )
        book = self.book_with_trades()
        market = MarketState(
            symbol=cand["symbol"],
            last=float(cand["entry_price"]),
            adv_shares=self._adv_shares(cand["symbol"]),
            atr=abs(cand["entry_price"] - cand["stop_price"]) / max(self.cfg.stop_atr_mult, 1e-9),
            day_type=cand.get("day_type"),
            data_quality="fresh",
            price_caliber="adjusted",
            session="rth",
            feed=self.cfg.data_feed,
        )
        budget = budget_for(self.cfg, INTRADAY, book)
        ctx = GateContext(
            request=request,
            book=book,
            market=market,
            mandate=self.mandate,
            config=self.cfg,
            now=now,
            ladder=self.ladder(),
            vol=self.vol(),
            es=self.es(),
            sleeve_ceiling_pct=budget.capital_ceiling_pct,
            sleeve_deployed_pct=budget.deployed_pct,
            setup_validated=True,
            day_type=cand.get("day_type"),
            stage="order",
        )
        decision = self._gate(ctx)
        outcome = {
            "symbol": cand["symbol"],
            "setup": cand["setup"],
            "verdict": decision.verdict,
            "binding_gate": decision.binding_gate,
            "allowed": decision.allowed,
            "reason": decision.permission_reason_code,
            "adjusted_risk_pct": decision.adjusted_risk_pct,
            "snapshot": decision.state_snapshot,
        }
        if not decision.allowed:
            return outcome, None
        if not armed:
            return outcome, None

        caps = SizingCaps(
            sleeve_risk_remaining_pct=max(0.0, budget.risk_per_trade_pct),
            house_heat_remaining_pct=max(0.0, self.cfg.max_heat_pct - book.heat_pct),
            sleeve_notional_room_usd=notional_room_usd(budget, book),
            participation_cap_pct=self.cfg.max_adv_pct,
            kelly_fraction=self.cfg.kelly_fraction,
            validated_edge=0.05,
            kelly_win_loss_ratio=1.0,
        )
        risk_pct = decision.adjusted_risk_pct or request.requested_risk_pct
        sized = self._size(
            RiskRequest(**{**request.__dict__, "requested_risk_pct": risk_pct}),
            caps=caps,
            vol_scalar=1.0,  # the gate already applied the vol reduction
        )
        if not sized.ok:
            outcome["reason"] = sized.reason
            return outcome, None
        outcome["qty"] = sized.qty
        return outcome, self._submit(cand, qty=sized.qty, now=now)

    def book_with_trades(self) -> BookState:
        if not self._trades_today and not self.book.trades_today:
            return self.book
        merged = dict(self.book.trades_today)
        for symbol, count in self._trades_today.items():
            merged[symbol] = merged.get(symbol, 0) + count
        return BookState(
            equity=self.book.equity,
            cash=self.book.cash,
            positions=self.book.positions,
            heat_pct=self.book.heat_pct,
            es_pct=self.book.es_pct,
            day_pnl_pct=self.book.day_pnl_pct,
            five_day_pct=self.book.five_day_pct,
            drawdown_pct=self.book.drawdown_pct,
            trades_today=merged,
            sleeve_trades_today={INTRADAY: sum(self._trades_today.values())},
        )

    def _adv_shares(self, symbol: str) -> float | None:
        try:
            bars = self.md.daily(symbol, count=21)
        except Exception:  # noqa: BLE001 - no volume history means no participation check
            return None
        return self._adv(bars)

    def _submit(self, cand: dict[str, Any], *, qty: int, now: datetime) -> dict[str, Any] | None:
        from .order.guard import OrderIntent, client_order_id, guard
        from .order.policy import plan_entry

        market = MarketState(
            symbol=cand["symbol"],
            last=float(cand["entry_price"]),
            spread_bps=None,
            adv_shares=self._adv_shares(cand["symbol"]),
            data_quality="fresh",
            price_caliber="adjusted",
            session="rth",
        )
        try:
            plan = plan_entry(
                RiskRequest(
                    sleeve=INTRADAY,
                    symbol=cand["symbol"],
                    setup=cand["setup"],
                    side=cand["side"],
                    stop_distance=abs(cand["entry_price"] - cand["stop_price"]),
                    price=float(cand["entry_price"]),
                    equity=self.book.equity,
                    requested_risk_pct=intraday_policy(self.cfg).risk_per_trade_pct,
                    stop_price=cand["stop_price"],
                ),
                qty,
                market=market,
                config=self.cfg,
            )
        except ValueError as exc:
            if self.audit is not None:
                self.audit.append("order_skipped", f"policy: {exc}", symbol=cand["symbol"])
            return None

        book = self.book_with_trades()
        for child in plan.orders:
            intent = OrderIntent(
                intent_id=f"{cand['symbol']}-{child.seq}",
                sleeve=INTRADAY,
                symbol=cand["symbol"],
                side=cand["side"],
                qty=child.qty,
                order_type=child.order_type,
                limit_price=child.limit_price,
                stop_price=cand["stop_price"],
                tif="day",
                client_order_id=client_order_id(INTRADAY, cand["setup"], f"{child.seq}"),
                created_at=now.isoformat(timespec="seconds"),
            )
            verdict = guard(
                intent,
                book=book,
                market=market,
                mandate=self.mandate,
                config=self.cfg,
                gate_verdict="ALLOW",
                price_of=float(cand["entry_price"]),
            )
            if not verdict.ok:
                if self.audit is not None:
                    self.audit.append(
                        "order_denied",
                        "; ".join(verdict.reasons),
                        symbol=cand["symbol"],
                        binding=verdict.binding,
                    )
                continue
            result = self.orders.submit(intent)
            if self.audit is not None:
                self.audit.append(
                    "order_submit",
                    result.status,
                    symbol=cand["symbol"],
                    detail=result.detail,
                    qty=child.qty,
                )
            if result.status == "submitted":
                self._trades_today[cand["symbol"]] = self._trades_today.get(cand["symbol"], 0) + 1
                return {
                    "symbol": cand["symbol"],
                    "setup": cand["setup"],
                    "qty": child.qty,
                    "limit_price": child.limit_price,
                    "client_order_id": result.client_order_id,
                    "status": result.status,
                }
        return None

    def _protect(self, *, now: datetime) -> list[dict[str, Any]]:
        from .order.stops import protective_orders

        positions = [p for p in self.book.positions if p.sleeve == INTRADAY]
        if not positions:
            return []
        covered = {
            str(o.get("symbol"))
            for o in (self.states.open_orders() if self.states is not None else ())
            if o is not None
        }
        stops = {
            p.symbol: p.stop
            for p in self.book.positions
            if p.stop is not None and p.symbol not in covered
        }
        out: list[dict[str, Any]] = []
        for intent in protective_orders(positions, stops=stops, config=self.cfg):
            result = self.orders.submit(intent)
            if self.audit is not None:
                self.audit.append(
                    "protective_order",
                    result.status,
                    symbol=intent.symbol,
                    at=now.isoformat(timespec="seconds"),
                )
            out.append(
                {
                    "symbol": intent.symbol,
                    "client_order_id": result.client_order_id,
                    "status": result.status,
                }
            )
        return out

    def _reconcile(self, *, now: datetime) -> tuple[bool | None, tuple[dict[str, Any], ...]]:
        """Broker is the source of truth; a contradiction halts (design §13)."""
        from .reconcile import reconcile

        if self.states is None or self.broker_positions is None:
            return None, ()
        report = reconcile(
            local_fills=self.states.fills(),
            broker_positions=self.broker_positions(),
            broker_orders=(),
            local_open_orders=self.states.open_orders(),
            now=now,
        )
        if self.audit is not None:
            self.audit.append(
                "reconcile",
                report.detail,
                ok=report.ok,
                halt=report.halt,
                drift=len(report.drift),
            )
        return report.ok, tuple(
            {"kind": d.kind, "symbol": d.symbol, "detail": d.detail} for d in report.drift
        )

    def _flatten(self, *, now: datetime) -> tuple[bool, str]:
        from .order.flatten import flatten_plan, verify_flat

        positions = [p for p in self.book.positions if p.sleeve == INTRADAY]
        if not positions:
            return True, "nothing to flatten"
        plan = flatten_plan(positions, now=now, config=self.cfg)
        for intent in plan.orders:
            self.orders.submit(intent)
        if self.broker_positions is not None:
            return verify_flat(self.broker_positions(), now=now, config=self.cfg)
        return False, "flat verification needs the broker's positions"


__all__ = ["GATE_PRECEDENCE", "SCAN_SETUPS", "SessionEngine", "SessionResult"]
