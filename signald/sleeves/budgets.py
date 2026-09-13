"""Per-sleeve capital and risk budgets (design §7.1, implementation plan §3/A2).

A :class:`SleeveBudget` is the *usage* half of a sleeve's allocation: the
configured ceiling plus what the book currently consumes. The arithmetic is
deliberately trivial so it can never disagree with the house gate:

    room_pct            = max(0, capital_ceiling_pct - deployed_pct)
    notional_room_usd   = max(0, room_pct * book.equity)

Heat is a **book** number (open risk / equity across all positions), so it is
*not* recorded per sleeve by the broker. The sleeve's share is apportioned by
its deployed notional share, which is the only sleeve attribution the fills
carry:

    heat_pct(sleeve) = book.heat_pct * sleeve_deployed(sleeve) / book.notional()

When the book holds nothing (``book.notional() == 0``) every sleeve's heat
share is 0.0 - absence of exposure is not a reason to invent one.

The ceilings themselves are validated by ``Config.__post_init__`` on every
construction path, so a budget can only ever see a legal pair.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..risk.state import INTRADAY, SLEEVES, SWING, BookState
from .intraday import intraday_policy
from .swing import SleevePolicy, swing_policy


@dataclass(frozen=True)
class SleeveBudget:
    """A sleeve's configured budget plus its current usage against it."""

    sleeve: str
    capital_ceiling_pct: float
    vol_target: float
    risk_per_trade_pct: float
    es_budget_pct: float
    daily_loss_soft_pct: float
    daily_loss_hard_pct: float
    max_positions: int
    max_trades_per_name: int
    deployed_pct: float
    heat_pct: float

    @property
    def room_pct(self) -> float:
        """Unused capital ceiling as a fraction of equity, never negative."""
        return max(0.0, self.capital_ceiling_pct - self.deployed_pct)

    @property
    def at_ceiling(self) -> bool:
        """True when the sleeve has no capital room left (at or over its ceiling)."""
        return self.room_pct <= 0.0


def _policy_for(cfg: Config, sleeve: str) -> SleevePolicy:
    if sleeve == SWING:
        return swing_policy(cfg)
    if sleeve == INTRADAY:
        return intraday_policy(cfg)
    raise ValueError(f"unknown sleeve: {sleeve!r}; expected one of {list(SLEEVES)}")


def _notional_share(book: BookState, sleeve: str) -> float:
    """The sleeve's share of the book's deployed notional (0.0 on an empty book)."""
    total = book.notional()
    if total <= 0.0:
        return 0.0
    return book.sleeve_deployed(sleeve) / total


def budget_for(cfg: Config, sleeve: str, book: BookState) -> SleeveBudget:
    """Build one sleeve's budget record from config + book state.

    Raises :class:`ValueError` for a sleeve name the router never emits.
    """
    policy = _policy_for(cfg, sleeve)
    if sleeve == SWING:
        ceiling = cfg.sleeve_swing_capital_pct
        vol_target = cfg.sleeve_swing_vol_target
    else:
        ceiling = cfg.sleeve_intraday_capital_pct
        vol_target = cfg.sleeve_intraday_vol_target
    return SleeveBudget(
        sleeve=sleeve,
        capital_ceiling_pct=ceiling,
        vol_target=vol_target,
        risk_per_trade_pct=policy.risk_per_trade_pct,
        es_budget_pct=cfg.es_budget_sleeve_pct,
        daily_loss_soft_pct=cfg.daily_loss_soft_pct,
        daily_loss_hard_pct=cfg.daily_loss_hard_pct,
        max_positions=policy.max_positions,
        max_trades_per_name=policy.max_trades_per_name,
        deployed_pct=book.deployed_pct(sleeve),
        heat_pct=book.heat_pct * _notional_share(book, sleeve),
    )


def budgets_for(cfg: Config, book: BookState) -> dict[str, SleeveBudget]:
    """Both sleeves' budgets for one decision point (the closed sleeve set)."""
    return {sleeve: budget_for(cfg, sleeve, book) for sleeve in SLEEVES}


def notional_room_usd(budget: SleeveBudget, book: BookState) -> float:
    """Capital still available under the ceiling, in dollars; never negative."""
    return max(0.0, budget.room_pct * book.equity)
