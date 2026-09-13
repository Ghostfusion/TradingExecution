"""Sleeve routing, policies and budgets: routing is closed, budgets never lie.

The router is the only producer of a sleeve name; the policies are pure config
mappings; a budget reports room and heat from the book without inventing
numbers. Formula assertions below are hand-worked in the comments.
"""

from __future__ import annotations

import pytest

from signald.config import load_config
from signald.risk.state import INTRADAY, SWING, BookState, Position
from signald.samples import build_sample, build_sample_v11
from signald.schema import parse_research_decision
from signald.sleeves.budgets import (
    budget_for,
    budgets_for,
    notional_room_usd,
)
from signald.sleeves.intraday import intraday_policy
from signald.sleeves.router import RouteError, route_intraday, route_research
from signald.sleeves.swing import SleevePolicy, swing_policy

pytestmark = pytest.mark.timeout(120)


def _cfg_from_env(tmp_path, environ: dict[str, str]):
    """A config from an explicit environment only - never the developer's .env."""
    return load_config(env_file=tmp_path / "absent.env", environ=environ)


def _book(*, equity: float = 100_000.0, positions=(), heat_pct: float = 0.0) -> BookState:
    return BookState(equity=equity, cash=0.0, positions=tuple(positions), heat_pct=heat_pct)


def _pos(symbol: str, qty: float, price: float, sleeve: str) -> Position:
    return Position(symbol=symbol, qty=qty, avg_entry=price, last=price, sleeve=sleeve)


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize("builder", [build_sample, build_sample_v11], ids=["legacy", "v1.1"])
def test_a_research_artifact_always_routes_to_the_swing_sleeve(builder, cfg):
    rd = parse_research_decision(builder())
    route = route_research(rd, cfg)
    assert route.sleeve == SWING
    assert route.source == "research"
    assert route.reason


def test_a_producer_may_not_choose_the_intraday_sleeve(cfg):
    rd = parse_research_decision(build_sample(sleeve=INTRADAY))
    with pytest.raises(RouteError):
        route_research(rd, cfg)


def test_a_producer_confirming_swing_is_harmless(cfg):
    rd = parse_research_decision(build_sample(sleeve=SWING))
    assert route_research(rd, cfg).sleeve == SWING


@pytest.mark.parametrize(
    ("config_flag", "caller_flag"),
    [(False, False), (True, False), (False, True)],
    ids=["neither", "config-only", "caller-only"],
)
def test_the_intraday_router_needs_both_opt_ins(tmp_path, config_flag, caller_flag):
    cfg = _cfg_from_env(
        tmp_path, {"TRADINGEXEC_INTRADAY_ENABLED": str(config_flag).lower()}
    )
    with pytest.raises(RouteError):
        route_intraday(cfg, enabled=caller_flag)


def test_the_intraday_router_starts_when_both_opt_ins_are_on(tmp_path):
    cfg = _cfg_from_env(tmp_path, {"TRADINGEXEC_INTRADAY_ENABLED": "true"})
    route = route_intraday(cfg, enabled=True)
    assert route.sleeve == INTRADAY
    assert route.source == "engine"


# --- policies --------------------------------------------------------------


def test_the_swing_policy_maps_the_swing_keys(cfg):
    policy = swing_policy(cfg)
    assert isinstance(policy, SleevePolicy)
    assert policy.sleeve == SWING
    assert policy.risk_per_trade_pct == cfg.risk_per_trade_swing_pct
    assert policy.max_positions == cfg.max_positions_swing
    assert policy.max_single_name_pct == cfg.max_single_name_pct_swing
    assert policy.max_trades_per_name == 0  # unlimited
    assert policy.entry_policy == "next_session_limit_band"
    assert policy.overnight is True
    assert policy.flat_by is None


def test_the_intraday_policy_maps_the_intraday_keys(tmp_path):
    cfg = _cfg_from_env(tmp_path, {"TRADINGEXEC_INTRADAY_MAX_TRADES_PER_NAME": "3"})
    policy = intraday_policy(cfg)
    assert policy.sleeve == INTRADAY
    assert policy.risk_per_trade_pct == cfg.risk_per_trade_intraday_pct
    assert policy.max_positions == cfg.max_positions_intraday
    assert policy.max_single_name_pct == cfg.max_single_name_pct_intraday
    assert policy.max_trades_per_name == 3
    assert policy.entry_policy == "intraday_windows"
    assert policy.overnight is False
    assert policy.flat_by == cfg.intraday_flat_by


@pytest.mark.parametrize(
    "policy_fn", [swing_policy, intraday_policy], ids=["swing", "intraday"]
)
def test_neither_policy_ever_uses_a_naked_stop(cfg, policy_fn):
    assert policy_fn(cfg).stop_kind == "synthetic_stop_limit"


# --- budgets ---------------------------------------------------------------


def test_room_is_zero_at_the_ceiling_and_never_negative_above_it(cfg):
    # hand-worked: 700 * 100 / 100,000 = 0.70 (exactly the swing ceiling);
    #              800 * 100 / 100,000 = 0.80 (over it).
    at = budget_for(cfg, SWING, _book(positions=(_pos("AAA", 700, 100.0, SWING),)))
    over = budget_for(cfg, SWING, _book(positions=(_pos("AAA", 800, 100.0, SWING),)))
    assert at.deployed_pct == pytest.approx(0.70)
    assert at.room_pct == pytest.approx(0.0)
    assert at.at_ceiling
    assert over.room_pct == 0.0
    assert over.room_pct >= 0.0
    assert over.at_ceiling


def test_notional_room_is_the_unused_ceiling_times_equity(cfg):
    # hand-worked: 300 * 200 = 60,000 = 0.30 of 200,000 equity;
    #              0.70 ceiling - 0.30 deployed = 0.40 room; 0.40 * 200,000 = 80,000.
    book = _book(equity=200_000.0, positions=(_pos("AAA", 300, 200.0, SWING),))
    budget = budget_for(cfg, SWING, book)
    assert budget.deployed_pct == pytest.approx(0.30)
    assert budget.room_pct == pytest.approx(0.40)
    assert notional_room_usd(budget, book) == pytest.approx(80_000.0)


def test_notional_room_is_zero_when_the_sleeve_is_over_its_ceiling(cfg):
    book = _book(positions=(_pos("AAA", 800, 100.0, SWING),))  # 0.80 > 0.70
    budget = budget_for(cfg, SWING, book)
    assert budget.room_pct == 0.0
    assert notional_room_usd(budget, book) == 0.0


def test_sleeve_heat_is_apportioned_by_deployed_notional_share(cfg):
    # hand-worked: swing 600 * 100 = 60,000, intraday 300 * 100 = 30,000, book 90,000;
    #              swing share 2/3 of 3% heat = 2%, intraday share 1/3 = 1%.
    book = _book(
        heat_pct=0.03,
        positions=(
            _pos("AAA", 600, 100.0, SWING),
            _pos("BBB", 300, 100.0, INTRADAY),
        ),
    )
    assert budget_for(cfg, SWING, book).heat_pct == pytest.approx(0.02)
    assert budget_for(cfg, INTRADAY, book).heat_pct == pytest.approx(0.01)


def test_an_empty_book_has_no_heat_to_apportion(cfg):
    assert budget_for(cfg, SWING, _book(heat_pct=0.03)).heat_pct == 0.0


def test_budgets_for_covers_every_sleeve(cfg):
    budgets = budgets_for(cfg, _book())
    assert set(budgets) == {SWING, INTRADAY}
    assert budgets[SWING].capital_ceiling_pct == cfg.sleeve_swing_capital_pct
    assert budgets[INTRADAY].capital_ceiling_pct == cfg.sleeve_intraday_capital_pct


@pytest.mark.parametrize("sleeve", ["options", "", "SWING"], ids=["unknown", "empty", "case"])
def test_an_unknown_sleeve_is_rejected(cfg, sleeve):
    with pytest.raises(ValueError):
        budget_for(cfg, sleeve, _book())


def test_budgets_for_works_for_the_default_seventy_thirty_split(tmp_path):
    cfg = _cfg_from_env(
        tmp_path,
        {
            "TRADINGEXEC_SLEEVE_SWING_CAPITAL_PCT": "0.7",
            "TRADINGEXEC_SLEEVE_INTRADAY_CAPITAL_PCT": "0.3",
        },
    )
    budgets = budgets_for(cfg, _book())
    assert budgets[SWING].capital_ceiling_pct == pytest.approx(0.70)
    assert budgets[INTRADAY].capital_ceiling_pct == pytest.approx(0.30)
    assert budgets[SWING].room_pct == pytest.approx(0.70)
    assert budgets[INTRADAY].room_pct == pytest.approx(0.30)
