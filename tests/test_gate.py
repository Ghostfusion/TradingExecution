"""The house risk gate: one blocking test per check, precedence, and mutations.

Every check in `GATE_PRECEDENCE` that can refuse an order has a test here that
makes it refuse. Two structural tests prove the properties the plan calls
non-negotiable: the binding gate is the **first** failure in precedence order,
and a check that *raises* blocks instead of passing.

The engines (tail/voltarget/ladder) are exercised by their own test modules;
here they are injected as prepared state, which is what the gate actually sees.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from signald.mandate import DEFAULT_MANDATE, load_mandate, write_mandate
from signald.risk import gate as G
from signald.risk.ladder import LadderState, rung_for
from signald.risk.state import INTRADAY, SWING, BookState, MarketState, Position, RiskRequest
from signald.risk.tail import ESResult
from signald.risk.voltarget import VolScalar

pytestmark = pytest.mark.timeout(120)


def request(**kw) -> RiskRequest:
    base = {
        "sleeve": SWING,
        "symbol": "AVGO",
        "setup": "VALUE_DIP",
        "side": "buy",
        "stop_distance": 5.0,
        "price": 100.0,
        "equity": 100_000.0,
        "requested_risk_pct": 0.005,
        "stop_price": 95.0,
        "measured_move_bps": 60.0,
        "round_trip_cost_bps": 12.0,
        "trailing_volume": 2_000_000.0,
        "planned_notional_usd": 5_000.0,
        "cluster": "semi",
    }
    base.update(kw)
    return RiskRequest(**base)


def book(**kw) -> BookState:
    base = {"equity": 100_000.0, "cash": 40_000.0}
    base.update(kw)
    return BookState(**base)


def market(**kw) -> MarketState:
    base = {
        "symbol": "AVGO",
        "last": 100.0,
        "spread_bps": 4.0,
        "spread_median_bps": 4.0,
        "quote_age_s": 0.5,
        "bar_age_s": 5.0,
        "feed": "sip",
        "session": "rth",
        "tradable": True,
        "shortable": True,
        "adv_shares": 5_000_000.0,
        "atr": 2.0,
        "data_quality": "fresh",
        "price_caliber": "adjusted",
    }
    base.update(kw)
    return MarketState(**base)


def ladder(rung: int = 0, **kw) -> LadderState:
    """The *real* ladder state for a rung - never a hand-copied table.

    Using `rung_for` keeps this test honest: if the ladder's rung semantics
    move, the gate tests move with them.
    """
    triggers = {
        0: {},
        1: {"day_pnl_pct": -0.01},
        2: {"day_pnl_pct": -0.03},
        3: {"five_day_pct": -0.06},
        4: {"drawdown_pct": -0.15},
    }[rung]
    state = rung_for(
        day_pnl_pct=float(triggers.get("day_pnl_pct", 0.0)),
        five_day_pct=float(triggers.get("five_day_pct", 0.0)),
        drawdown_pct=float(triggers.get("drawdown_pct", 0.0)),
        soft_pct=0.01,
        hard_pct=0.03,
        derisk_5d_pct=0.06,
        derisk_dd_pct=0.10,
        halt_dd_pct=0.15,
    )
    assert state.rung == rung, f"ladder factory drift: wanted rung {rung}, got {state.rung}"
    return replace(state, **kw) if kw else state


def vol(scalar: float = 1.0, **kw) -> VolScalar:
    base = {
        "scalar": scalar,
        "target": 0.10,
        "realized": 0.10 / max(scalar, 1e-9),
        "warmup_ok": True,
        "applied": scalar != 1.0,
        "reason": "applied" if scalar != 1.0 else "within_band",
    }
    base.update(kw)
    return VolScalar(**base)


def es(value_pct: float = 0.004, **kw) -> ESResult:
    base = {
        "value_pct": value_pct,
        "estimator": "historical",
        "flagged": False,
        "window_days": 500,
        "components": {"historical": value_pct, "parametric": 0.003, "stress": 0.006},
    }
    base.update(kw)
    return ESResult(**base)


def ctx(mandate, cfg, **kw) -> G.GateContext:
    base = {
        "request": request(),
        "book": book(),
        "market": market(),
        "mandate": mandate,
        "config": cfg,
        "now": cfg.now(),
        "ladder": ladder(),
        "vol": vol(),
        "es": es(),
        "sleeve_ceiling_pct": 0.70,
        "sleeve_deployed_pct": 0.0,
        "setup_validated": True,
        "day_type": None,
    }
    base.update(kw)
    return G.GateContext(**base)


@pytest.fixture()
def mandate(cfg, now):
    """A mandate that never expires during the suite's life (no wall-clock pin)."""
    raw = dict(DEFAULT_MANDATE)
    raw["created"] = now.date().isoformat()
    raw["expires"] = (now + timedelta(days=3650)).date().isoformat()
    write_mandate(cfg.mandate_path, raw)
    return load_mandate(cfg.mandate_path)


@pytest.fixture()
def gate_cfg(cfg, now):
    """The conftest config plus a `now` at 10:00 so the intraday window is open."""
    return replace(cfg, now_fn=lambda: now.replace(hour=10, minute=0))


def run(mandate, cfg, **kw):
    return G.evaluate(ctx(mandate, cfg, **kw))


# --- mandate ---------------------------------------------------------------
def test_symbol_outside_the_mandate_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, request=request(symbol="TSLA"))
    assert d.verdict == "BLOCK" and d.binding_gate == "mandate"
    assert d.permission_reason_code == "symbol"


def test_cash_below_reserve_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, book=book(cash=1_000.0))
    assert d.verdict == "BLOCK" and d.permission_reason_code == "cash_reserve"


def test_expired_mandate_blocks(mandate, gate_cfg, now):
    expired = replace(mandate, expires=now.date() - timedelta(days=1))
    d = run(expired, gate_cfg)
    assert d.verdict == "BLOCK" and d.permission_reason_code == "expired"


def test_leverage_cap_blocks(mandate, gate_cfg):
    # equity 80k -> leverage cap 120k, below the mandate's 150k exposure cap, so
    # the leverage rule is the one that binds
    heavy = book(
        equity=80_000.0,
        cash=40_000.0,
        positions=(Position(symbol="MSFT", qty=1_180, avg_entry=100.0, last=100.0),),
    )
    d = run(mandate, gate_cfg, book=heavy, request=request(equity=80_000.0))
    assert d.verdict == "BLOCK" and d.permission_reason_code == "leverage_cap"


# --- sleeve capital --------------------------------------------------------
def test_sleeve_at_its_ceiling_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, sleeve_deployed_pct=0.70)
    assert d.verdict == "BLOCK" and d.binding_gate == "sleeve_capital"
    assert d.permission_reason_code == "sleeve_ceiling"


def test_a_request_over_the_sleeve_room_is_reduced_to_fit(mandate, gate_cfg):
    # room = 0.70*100k - 0.69*100k = 1,000 USD; notional per risk = 100k*100/5 = 2,000,000
    # -> adjusted risk = 1,000 / 2,000,000 = 0.0005
    d = run(mandate, gate_cfg, sleeve_deployed_pct=0.69)
    assert d.verdict == "REDUCE" and d.binding_gate == "sleeve_capital"
    assert d.adjusted_risk_pct == pytest.approx(0.0005)


# --- drawdown ladder -------------------------------------------------------
def test_halt_blocks_everything(mandate, gate_cfg):
    d = run(mandate, gate_cfg, ladder=ladder(4))
    assert d.verdict == "BLOCK" and d.binding_gate == "house_drawdown"
    assert d.permission_reason_code == "halt"


def test_rung_one_blocks_only_new_intraday_entries(mandate, gate_cfg):
    swing = run(mandate, gate_cfg, ladder=ladder(1))
    assert swing.verdict == "ALLOW"
    intra = run(
        mandate,
        gate_cfg,
        ladder=ladder(1),
        request=request(sleeve=INTRADAY, setup="ORB_RVOL", planned_notional_usd=2_000.0),
        day_type="trend",
        sleeve_ceiling_pct=0.30,
    )
    assert intra.verdict == "BLOCK" and intra.permission_reason_code == "no_new_intraday"


def test_rung_three_halves_size(mandate, gate_cfg):
    d = run(mandate, gate_cfg, ladder=ladder(3))
    assert d.verdict == "REDUCE" and d.binding_gate == "house_drawdown"
    assert d.adjusted_risk_pct == pytest.approx(0.0025)


# --- ES budget -------------------------------------------------------------
def test_es_over_budget_reduces_to_the_room(mandate, gate_cfg):
    # budget = min(house 1.0%, sleeve 1.5%) = 1.0%; es 0.8% -> adjusted 0.2%
    d = run(mandate, gate_cfg, es=es(0.008))
    assert d.verdict == "REDUCE" and d.binding_gate == "house_cvar"
    assert d.adjusted_risk_pct == pytest.approx(0.002)


def test_es_already_over_budget_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, es=es(0.012))
    assert d.verdict == "BLOCK" and d.binding_gate == "house_cvar"
    assert d.permission_reason_code == "es_budget"


# --- correlation stress ----------------------------------------------------
def test_cluster_cap_blocks(mandate, gate_cfg):
    other = Position(symbol="MSFT", qty=60, avg_entry=100.0, last=100.0, cluster="semi")
    # cap = 0.25 * 0.70 * 100k = 17,500; existing 6,000 + 5,000 planned = 11,000 -> ok
    assert run(mandate, gate_cfg, book=book(positions=(other,))).verdict == "ALLOW"
    big = replace(mandate, max_notional_per_order_usd=50_000.0)
    d = run(
        big,
        gate_cfg,
        book=book(positions=(other,)),
        request=request(planned_notional_usd=12_000.0, requested_risk_pct=0.012),
    )
    assert d.verdict == "BLOCK" and d.binding_gate == "correlation_stress"


# --- vol regime ------------------------------------------------------------
def test_vol_above_target_reduces(mandate, gate_cfg):
    d = run(mandate, gate_cfg, vol=vol(0.5))
    assert d.verdict == "REDUCE" and d.binding_gate == "vol_regime"
    assert d.adjusted_risk_pct == pytest.approx(0.0025)


def test_vol_warmup_never_scales(mandate, gate_cfg):
    d = run(mandate, gate_cfg, vol=vol(1.0, warmup_ok=False, reason="warmup"))
    assert d.verdict == "ALLOW"


def test_a_scalar_that_zeroes_the_risk_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, vol=vol(0.0))
    assert d.verdict == "BLOCK" and d.permission_reason_code == "vol_scalar_zero"


# --- market regime ---------------------------------------------------------
def test_breakout_setup_needs_a_trend_day(mandate, gate_cfg):
    intraday = {
        "request": request(sleeve=INTRADAY, setup="ORB_RVOL", planned_notional_usd=2_000.0),
        "sleeve_ceiling_pct": 0.30,
    }
    unknown = run(mandate, gate_cfg, day_type=None, **intraday)
    assert unknown.permission_reason_code == "day_type_unknown"
    ranged = run(mandate, gate_cfg, day_type="range", **intraday)
    assert ranged.permission_reason_code == "setup_regime"
    assert run(mandate, gate_cfg, day_type="trend", **intraday).verdict != "BLOCK"


def test_a_swing_setup_has_no_day_type_requirement(mandate, gate_cfg):
    assert run(mandate, gate_cfg, day_type=None).verdict == "ALLOW"


# --- knife guard -----------------------------------------------------------
def test_news_shock_blocks_mean_reversion_only(mandate, gate_cfg):
    reversion = request(setup="VWAP_REVERT", sleeve=INTRADAY, planned_notional_usd=2_000.0)
    d = run(
        mandate,
        gate_cfg,
        market=market(news_shock=True),
        request=reversion,
        day_type="range",
        sleeve_ceiling_pct=0.30,
    )
    assert d.verdict == "BLOCK" and d.binding_gate == "knife_guard"
    assert d.permission_reason_code == "news_shock"
    assert run(mandate, gate_cfg, market=market(news_shock=True)).verdict == "ALLOW"


def test_reopen_cooldown_blocks_a_reversion_entry(mandate, gate_cfg):
    d = run(
        mandate,
        gate_cfg,
        market=market(reopen_cooldown_s=60.0),
        request=request(setup="VWAP_REVERT", sleeve=INTRADAY, planned_notional_usd=2_000.0),
        day_type="range",
        sleeve_ceiling_pct=0.30,
    )
    assert d.permission_reason_code == "reopen_cooldown"


def test_adverse_impulse_blocks_a_falling_knife(mandate, gate_cfg):
    d = run(
        mandate,
        gate_cfg,
        market=market(impulse_pct=-0.05),  # -5% vs 2x ATR% (4%)
        request=request(setup="VWAP_REVERT", sleeve=INTRADAY, planned_notional_usd=2_000.0),
        day_type="range",
        sleeve_ceiling_pct=0.30,
    )
    assert d.permission_reason_code == "adverse_impulse"


# --- concentration ---------------------------------------------------------
def test_single_name_cap_reduces_then_blocks(mandate, gate_cfg):
    held = Position(symbol="AVGO", qty=60, avg_entry=100.0, last=100.0)
    d = run(mandate, gate_cfg, book=book(positions=(held,)))
    assert d.verdict == "REDUCE" and d.binding_gate == "concentration"
    assert d.adjusted_risk_pct == pytest.approx(0.002)  # room 4,000 / 2,000,000
    full = Position(symbol="AVGO", qty=100, avg_entry=100.0, last=100.0)
    assert run(mandate, gate_cfg, book=book(positions=(full,))).permission_reason_code == (
        "single_name_cap"
    )


def test_max_positions_blocks_a_new_name(mandate, gate_cfg):
    held = tuple(
        Position(symbol=s, qty=10, avg_entry=100.0, last=100.0)
        for s in ("MSFT", "GOOG", "NVDA", "QCOM", "BAC", "SPY", "GLD", "AVGO")
    )
    wider = replace(mandate, allowed=frozenset(mandate.allowed | {"TSLA"}))
    d = run(wider, gate_cfg, book=book(positions=held), request=request(symbol="TSLA"))
    assert d.verdict == "BLOCK" and d.permission_reason_code == "max_positions"


def test_adding_to_a_held_name_is_governed_by_the_name_cap(mandate, gate_cfg):
    """Eight positions held, but this is not a ninth name: the count rule is silent."""
    held = tuple(
        Position(symbol=s, qty=10, avg_entry=100.0, last=100.0)
        for s in ("MSFT", "GOOG", "NVDA", "QCOM", "BAC", "SPY", "GLD", "AVGO")
    )
    d = run(mandate, gate_cfg, book=book(positions=held), request=request(symbol="MSFT"))
    assert d.verdict != "BLOCK" or d.permission_reason_code != "max_positions"


def test_intraday_max_trades_per_name_blocks(mandate, gate_cfg):
    d = run(
        mandate,
        gate_cfg,
        book=book(trades_today={"AVGO": 1}),
        request=request(sleeve=INTRADAY, setup="ORB_RVOL", planned_notional_usd=2_000.0),
        day_type="trend",
        sleeve_ceiling_pct=0.30,
    )
    assert d.permission_reason_code == "max_trades_per_name"


# --- liquidity -------------------------------------------------------------
def test_unknown_adv_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, market=market(adv_shares=None))
    assert d.verdict == "BLOCK" and d.permission_reason_code == "no_adv"


def test_participation_cap_reduces(mandate, gate_cfg):
    # 1M book: participation room = 25% * 2,000 sh * $100 = 50,000 USD, inside the
    # single-name (100k) and cluster (175k) caps, so `liquidity` is what binds
    roomy = replace(
        mandate, max_notional_per_order_usd=1_000_000.0, max_total_exposure_usd=5_000_000.0
    )
    d = run(
        roomy,
        gate_cfg,
        book=book(equity=1_000_000.0, cash=500_000.0),
        market=market(adv_shares=2_000.0),
        request=request(
            planned_notional_usd=80_000.0, requested_risk_pct=0.005, equity=1_000_000.0
        ),
    )
    assert d.verdict == "REDUCE" and d.binding_gate == "liquidity"
    assert d.adjusted_risk_pct == pytest.approx(0.0025)


def test_spread_blowout_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, market=market(spread_bps=20.0, spread_median_bps=4.0))
    assert d.permission_reason_code == "spread_blowout"


# --- cost gate -------------------------------------------------------------
def test_cost_gate_blocks_an_uneconomic_setup(mandate, gate_cfg):
    d = run(mandate, gate_cfg, request=request(measured_move_bps=30.0))
    assert d.verdict == "BLOCK" and d.binding_gate == "cost"
    assert d.permission_reason_code == "cost_gate"


def test_unknown_expected_move_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, request=request(measured_move_bps=None))
    assert d.permission_reason_code == "no_move"


def test_round_trip_cost_defaults_by_liquidity(mandate, gate_cfg):
    """A low-float name (below min ADV) pays the wider band: 3 x 30 = 90bps."""
    d = run(
        mandate,
        gate_cfg,
        market=market(adv_shares=100_000.0),
        request=request(round_trip_cost_bps=None, measured_move_bps=60.0),
    )
    assert d.verdict == "BLOCK" and d.permission_reason_code == "cost_gate"


# --- wash / shortability ---------------------------------------------------
def test_self_cross_with_the_other_sleeve_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, other_sleeve_side="sell")
    assert d.verdict == "BLOCK" and d.binding_gate == "wash"
    netting = run(mandate, gate_cfg, other_sleeve_side="sell", wash_nets=True)
    assert netting.verdict == "ALLOW"


def test_shorts_require_a_shortable_name(mandate, gate_cfg):
    short = request(side="sell", opens_short=True, setup="VWAP_REVERT")
    first = run(mandate, gate_cfg, request=short).permission_reason_code
    assert first in {"shorts", "shorts_off"}  # mandate owns the first word on shorts
    permissive = replace(mandate, shorts=True)
    # VWAP_REVERT is a range-day setup: give the classifier a range so the regime
    # check passes and the shortability rules are what speak
    assert (
        run(
            permissive,
            gate_cfg,
            request=short,
            market=market(shortable=False),
            day_type="range",
        ).permission_reason_code
        == "not_etb"
    )
    assert (
        run(permissive, gate_cfg, request=short, market=market(ssr=True), day_type="range")
        .permission_reason_code
        == "ssr"
    )


# --- data ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mk", "code"),
    [
        (lambda: market(data_quality="partial"), "data_quality_partial"),
        (lambda: market(quote_age_s=9.0), "stale_quote"),
        (lambda: market(bar_age_s=600.0), "stale_bar"),
        (lambda: market(price_caliber="mixed"), "price_caliber"),
    ],
)
def test_data_quality_blocks(mandate, gate_cfg, mk, code):
    d = run(mandate, gate_cfg, market=mk())
    assert d.verdict == "BLOCK" and d.binding_gate == "data"
    assert d.permission_reason_code == code


def test_a_missing_stop_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, request=request(stop_price=None))
    assert d.permission_reason_code == "no_stop"


# --- time / halt -----------------------------------------------------------
def test_a_halt_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, market=market(halted=True, halt_reason="LULD"))
    assert d.verdict == "BLOCK" and d.binding_gate == "halt"
    assert d.permission_reason_code == "halted"


def test_a_closed_session_blocks(mandate, gate_cfg):
    d = run(mandate, gate_cfg, market=market(session="closed"))
    assert d.verdict == "BLOCK" and d.permission_reason_code == "session"


@pytest.mark.parametrize(
    ("when", "code"),
    [((9, 20), "too_early"), ((11, 30), "too_late"), ((15, 55), "after_flat_by")],
)
def test_the_intraday_entry_window_is_enforced(mandate, cfg, now, when, code):
    clock = replace(cfg, now_fn=lambda: now.replace(hour=when[0], minute=when[1]))
    d = run(
        mandate,
        clock,
        request=request(sleeve=INTRADAY, setup="ORB_RVOL", planned_notional_usd=2_000.0),
        day_type="trend",
        sleeve_ceiling_pct=0.30,
    )
    assert d.verdict == "BLOCK" and d.binding_gate == "time"
    assert d.permission_reason_code == code


def test_inside_the_window_the_intraday_entry_is_allowed(mandate, cfg, now):
    clock = replace(cfg, now_fn=lambda: now.replace(hour=9, minute=40))
    d = run(
        mandate,
        clock,
        request=request(sleeve=INTRADAY, setup="ORB_RVOL", planned_notional_usd=2_000.0),
        day_type="trend",
        sleeve_ceiling_pct=0.30,
    )
    assert d.verdict == "ALLOW"


# --- approval --------------------------------------------------------------
def test_a_large_notional_needs_approval_and_holds_the_order(mandate, gate_cfg):
    # Raise the per-order cap and drop the approval factor so the threshold (20k)
    # is the only rule that fires: 25k sits inside every size cap on a 1M book.
    roomy = replace(
        mandate, max_notional_per_order_usd=100_000.0, max_total_exposure_usd=5_000_000.0
    )
    cfg = replace(gate_cfg, approval_threshold_factor=0.2)
    big = book(equity=1_000_000.0, cash=500_000.0)
    d = run(
        roomy,
        cfg,
        book=big,
        request=request(planned_notional_usd=25_000.0, equity=1_000_000.0),
    )
    assert d.verdict == "REDUCE" and d.binding_gate == "approval"
    assert d.approval_required is True
    assert d.permission_reason_code == "approval_required"


# --- structure -------------------------------------------------------------
def test_the_binding_gate_is_the_first_failure_in_precedence_order(mandate, gate_cfg):
    # both the liquidity check and the data check fail; `liquidity` precedes `data`
    d = run(
        mandate,
        gate_cfg,
        market=market(adv_shares=None, price_caliber="mixed"),
    )
    assert d.verdict == "BLOCK" and d.binding_gate == "liquidity"
    assert any(f.check == "data" for f in d.failures)  # all failures are reported


def test_a_check_that_raises_blocks(monkeypatch, mandate, gate_cfg):
    def boom(_):
        raise RuntimeError("engine exploded")

    monkeypatch.setitem(G.CHECKS, "knife_guard", boom)
    d = run(mandate, gate_cfg)
    assert d.verdict == "BLOCK" and d.binding_gate == "knife_guard"
    assert d.permission_reason_code == "check_error"


def test_multiple_reductions_report_the_smallest_adjusted_risk(mandate, gate_cfg):
    d = run(mandate, gate_cfg, vol=vol(0.4), ladder=ladder(2))
    # house_drawdown precedes vol_regime; ladder 2 keeps the swing sleeve at x0.5
    # (0.0025) while vol x0.4 gives 0.0020, so the smallest adjusted risk wins
    assert d.verdict == "REDUCE" and d.binding_gate == "house_drawdown"
    assert d.adjusted_risk_pct == pytest.approx(0.002)


def test_all_clear_is_allow_with_no_binding_gate(mandate, gate_cfg):
    d = run(mandate, gate_cfg)
    assert d.verdict == "ALLOW" and d.binding_gate is None
    assert d.reasons == ("all checks passed",)
    assert d.adjusted_risk_pct is None and d.approval_required is False


def test_the_snapshot_carries_the_measured_numbers(mandate, gate_cfg):
    d = run(mandate, gate_cfg, es=es(0.006), ladder=ladder(3), vol=vol(0.8))
    snap = d.state_snapshot
    assert snap["equity"] == 100_000.0
    assert snap["es_975_1d_pct"] == pytest.approx(0.006)
    assert snap["ladder_rung"] == 3
    assert snap["vol_scalar"] == pytest.approx(0.8)
    assert snap["projected_notional_usd"] == 5_000.0
    assert d.config_hash == gate_cfg.config_hash()


def test_research_risk_context_is_advisory_only(mandate, gate_cfg):
    """A research verdict of BLOCK changes nothing here - it is recorded, not read."""
    d = run(
        mandate,
        gate_cfg,
        research_risk_context={"risk_gate": {"verdict": "BLOCK"}, "regime": "risk-off"},
    )
    assert d.verdict == "ALLOW"
    assert d.state_snapshot["advisory_research_regime"] == "risk-off"


def test_the_projection_falls_back_to_risk_times_price_over_stop():
    """No planned notional: equity * risk * price / stop_distance = 10,000."""
    req = request(planned_notional_usd=None, requested_risk_pct=0.005, stop_distance=5.0)
    assert G.projected_notional(req) == pytest.approx(10_000.0)
