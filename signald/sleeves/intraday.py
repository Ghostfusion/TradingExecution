"""Intraday sleeve policy (design §3.3/§5, implementation plan §5).

Same contract as :mod:`signald.sleeves.swing`: a pure config mapping with no
market-state logic. The intraday sleeve never holds overnight risk and is
flattened by ``flat_by``; entries are confined to the intraday windows
(design §10, plan §5).
"""

from __future__ import annotations

from ..config import Config
from ..risk.state import INTRADAY
from .swing import STOP_KIND, SleevePolicy


def intraday_policy(cfg: Config) -> SleevePolicy:
    """Map the intraday config keys onto the policy record (design §3.3)."""
    return SleevePolicy(
        sleeve=INTRADAY,
        risk_per_trade_pct=cfg.risk_per_trade_intraday_pct,
        max_positions=cfg.max_positions_intraday,
        max_single_name_pct=cfg.max_single_name_pct_intraday,
        max_trades_per_name=cfg.intraday_max_trades_per_name,
        stop_kind=STOP_KIND,
        entry_policy="intraday_windows",
        overnight=False,
        flat_by=cfg.intraday_flat_by,
    )
