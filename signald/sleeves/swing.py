"""Swing sleeve policy (design §3.2/§3.5, implementation plan §4).

A policy is a *pure config mapping*: it names the risk constants and the
execution shape a sleeve is allowed to use. No value here is derived from
market state - the sizer and the house gate own every computed number
("one computation", design §1.2).

Both sleeves are fixed to ``synthetic_stop_limit``: a naked stop-market is
prohibited everywhere in the order path (design §10, plan §8.5).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..risk.state import SWING

#: The only stop shape either sleeve may use; never a naked stop-market.
STOP_KIND = "synthetic_stop_limit"


@dataclass(frozen=True)
class SleevePolicy:
    """Static, config-derived rules for one sleeve (no market state)."""

    sleeve: str
    risk_per_trade_pct: float
    max_positions: int
    max_single_name_pct: float
    max_trades_per_name: int
    stop_kind: str
    entry_policy: str
    overnight: bool
    flat_by: str | None


def swing_policy(cfg: Config) -> SleevePolicy:
    """Map the swing config keys onto the policy record (design §3.2)."""
    return SleevePolicy(
        sleeve=SWING,
        risk_per_trade_pct=cfg.risk_per_trade_swing_pct,
        max_positions=cfg.max_positions_swing,
        max_single_name_pct=cfg.max_single_name_pct_swing,
        # 0 means unlimited: the swing sleeve is not day-trade capped.
        max_trades_per_name=0,
        stop_kind=STOP_KIND,
        entry_policy="next_session_limit_band",
        overnight=True,
        flat_by=None,
    )
