"""Typed gate inputs: the book, the market, and the risk request (plan §4.1, §2.4).

Everything the house risk gate, the sizer, the order guard and the reconciler
look at is one of these three records. They are pure data with no I/O: the
daemon fills them from the broker/reconcile pass, the sleeves fill the request,
and every decision downstream is then a pure function of typed inputs
("deterministic numbers", design §1.2).

Exposure is computed from **fills** (positions), never from targets, and
``BookState.by_symbol`` nets both sleeves' legs so a self-cross is visible
before submission (design §3.5 R4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: Sleeve names (closed; the router is the only producer).
SWING = "swing"
INTRADAY = "intraday"
SLEEVES = (SWING, INTRADAY)


@dataclass(frozen=True)
class Position:
    """One open position as the broker reports it (fills, not targets)."""

    symbol: str
    qty: float
    avg_entry: float
    sleeve: str = SWING
    last: float | None = None
    stop: float | None = None
    setup: str | None = None
    sector: str | None = None
    cluster: str | None = None
    opened_at: str | None = None

    @property
    def notional(self) -> float:
        """Absolute market value using the broker's mark when present."""
        price = self.last if self.last is not None else self.avg_entry
        return abs(self.qty) * float(price)

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def open_risk_usd(self) -> float:
        """Distance to the protective stop times size (0 when no stop exists)."""
        if self.stop is None or self.last is None:
            return 0.0
        return abs(self.last - self.stop) * abs(self.qty)

    def unrealized_pct(self) -> float | None:
        if self.last is None or not self.avg_entry:
            return None
        return (self.last - self.avg_entry) / self.avg_entry * (1.0 if self.qty > 0 else -1.0)


@dataclass(frozen=True)
class BookState:
    """House-level state for one decision point (plan §4.1)."""

    equity: float
    cash: float
    positions: tuple[Position, ...] = ()
    heat_pct: float = 0.0
    es_pct: float = 0.0
    day_pnl_pct: float = 0.0
    five_day_pct: float = 0.0
    drawdown_pct: float = 0.0
    peak_equity: float | None = None
    net_beta: float = 0.0
    trades_today: dict[str, int] = field(default_factory=dict)
    sleeve_trades_today: dict[str, int] = field(default_factory=dict)
    open_orders: tuple[dict[str, Any], ...] = ()
    buying_power: float | None = None

    # -- exposure --------------------------------------------------------
    def by_symbol(self) -> dict[str, float]:
        """Net signed quantity per symbol across both sleeves (fills-based)."""
        net: dict[str, float] = {}
        for p in self.positions:
            net[p.symbol] = net.get(p.symbol, 0.0) + float(p.qty)
        return net

    def sleeve_deployed(self, sleeve: str) -> float:
        """Deployed notional of one sleeve (both legs, absolute)."""
        return float(sum(p.notional for p in self.positions if p.sleeve == sleeve))

    def deployed_pct(self, sleeve: str) -> float:
        return self.sleeve_deployed(sleeve) / self.equity if self.equity > 0 else 0.0

    def notional(self) -> float:
        return float(sum(p.notional for p in self.positions))

    def position_for(self, symbol: str) -> float:
        return float(self.by_symbol().get(symbol, 0.0))

    def cluster_notional(self) -> dict[str, float]:
        """Notional per correlation cluster (``cluster`` else ``sector`` else symbol)."""
        out: dict[str, float] = {}
        for p in self.positions:
            key = p.cluster or p.sector or p.symbol
            out[key] = out.get(key, 0.0) + p.notional
        return out

    def count(self, sleeve: str | None = None) -> int:
        if sleeve is None:
            return len(self.positions)
        return sum(1 for p in self.positions if p.sleeve == sleeve)

    def trades_for(self, symbol: str) -> int:
        return int(self.trades_today.get(symbol, 0))


@dataclass(frozen=True)
class MarketState:
    """Per-instrument market state, freshness-stamped (plan §4.1, design §9)."""

    symbol: str
    last: float | None = None
    spread_bps: float | None = None
    spread_median_bps: float | None = None
    quote_age_s: float | None = None
    bar_age_s: float | None = None
    feed: str = "sip"
    as_of: datetime | None = None
    session: str | None = None
    halted: bool = False
    halt_reason: str | None = None
    reopen_cooldown_s: float | None = None
    tradable: bool = True
    shortable: bool = False
    ssr: bool = False
    adv_shares: float | None = None
    atr: float | None = None
    day_type: str | None = None
    vol_regime: str | None = None
    data_quality: str = "unknown"
    price_caliber: str | None = None
    impulse_pct: float | None = None
    news_shock: bool = False
    last_bar_high: float | None = None
    last_bar_low: float | None = None

    def spread_blown(self, multiple: float = 3.0) -> bool:
        if self.spread_bps is None or not self.spread_median_bps:
            return False
        return self.spread_bps > multiple * self.spread_median_bps


@dataclass(frozen=True)
class RiskRequest:
    """A sleeve's *risk* request - never a quantity (design §3.5 R3, plan §4.3)."""

    sleeve: str
    symbol: str
    setup: str
    side: str  # "buy" | "sell"
    stop_distance: float
    price: float
    equity: float
    requested_risk_pct: float
    stop_price: float | None = None
    target_price: float | None = None
    measured_move_bps: float | None = None
    round_trip_cost_bps: float | None = None
    trailing_volume: float | None = None
    kelly_edge: float | None = None
    kelly_win_loss_ratio: float | None = None
    entry_reason: str | None = None
    intent_id: str | None = None
    #: Correlation cluster / sector of the *new* position (the gate's cluster check).
    cluster: str | None = None
    sector: str | None = None
    #: True only when this order *opens* a short. A `sell` that reduces a long
    #: (a research REDUCE/EXIT) is not a short intent and must not be asked for
    #: short permission - deriving this from `side` alone conflated the two.
    opens_short: bool = False
    #: Notional the sleeve intends, when it has one (a research target weight).
    #: When absent the gate projects it from the requested risk and the stop
    #: distance - the same relation the sizer enforces, using the *uncapped*
    #: request, so the projection is conservative.
    planned_notional_usd: float | None = None

    @property
    def notional_per_share(self) -> float:
        return abs(float(self.price))

    def implies_short(self) -> bool:
        return bool(self.opens_short)
