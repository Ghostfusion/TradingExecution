"""P4 harness tests: replay, costs, and the scorecard (plan §10, design §11.2).

Every expected number is worked by hand in the test body. The gate and the sizer
are never re-implemented here; the ALLOW stubs are deliberately minimal while
the BLOCK test drives the *real* ``signald.risk.gate.evaluate``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from signald.mandate import DEFAULT_MANDATE, load_mandate, write_mandate
from signald.risk.gate import GateContext, GateDecision, evaluate
from signald.risk.ladder import rung_for
from signald.risk.sizing import SizingCaps
from signald.risk.tail import ESResult
from signald.risk.voltarget import VolScalar
from signald.validation.harness import (
    TRADE_ROW_KEYS,
    HarnessCosts,
    replay,
    summarize,
    trade_row,
)

pytestmark = pytest.mark.timeout(120)

T0 = datetime(2026, 1, 5, 10, 0)


def bar(ts, o, h, low, c, **over):
    base = {
        "ts": ts,
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": 1_000_000.0,
    }
    base.update(over)
    return base


def signal_bars():
    """Three bars: signal, entry bar (open 100), stop bar (low 94)."""
    return [
        bar(T0, 100, 101, 99, 100, signal=True, setup="ORB", stop=95, target=110),
        bar(T0 + timedelta(minutes=1), 100, 101, 99.5, 100.5),
        bar(T0 + timedelta(minutes=2), 100, 100, 94, 95),
    ]


def allow_gate(context):
    return GateDecision(
        verdict="ALLOW",
        binding_gate=None,
        reasons=("all checks passed",),
        state_snapshot={},
        decided_at=context.ts.isoformat(),
        config_hash=context.config.config_hash(),
    )


def caps_factory(context):
    return SizingCaps(
        sleeve_risk_remaining_pct=0.02,
        house_heat_remaining_pct=0.03,
        sleeve_notional_room_usd=1_000_000.0,
        participation_cap_pct=1.0,
        kelly_fraction=0.25,
        validated_edge=0.5,
        kelly_win_loss_ratio=2.0,
        kelly_shrink=1.0,
    )


def run(bars, cfg, *, costs=None, gate=None, **over):
    return replay(
        bars_by_symbol={"AAPL": bars},
        starting_equity=100_000.0,
        config=cfg,
        gate_factory=gate or allow_gate,
        sizing_caps_factory=caps_factory,
        costs=costs or HarnessCosts(spread_bps=0.0),
        **over,
    )


# --- exits, worked by hand -------------------------------------------------
def test_stop_exit_is_minus_one_r(cfg):
    """Entry 100 (next open), stop 95 => risk/share 5; qty = 100k*0.005/5 = 100.
    Exit at 95 => gross = 100*(95-100) = -500 = -1R exactly; zero costs."""
    result = run(signal_bars(), cfg)
    assert len(result.trades) == 1
    row = result.trades[0]
    assert row["exit_reason"] == "stop"
    assert row["exit_px"] == 95.0
    assert row["qty"] == 100
    assert row["r_multiple"] == -1.0


def test_target_exit_is_two_r(cfg):
    """Entry 100, target 110, stop 95 => (110-100)*100 / (100*5) = +2R."""
    bars = signal_bars()
    bars[2] = bar(T0 + timedelta(minutes=2), 101, 112, 99, 111)
    row = run(bars, cfg).trades[0]
    assert row["exit_reason"] == "target"
    assert row["exit_px"] == 110.0
    assert row["r_multiple"] == 2.0


def test_flat_by_exit(cfg):
    """No stop/target hit; the 10:02 bar is at flat_by => exit at its close (103).
    gross = 100*(103-100) = 300 => +0.6R."""
    bars = signal_bars()
    bars[1] = bar(T0 + timedelta(minutes=1), 100, 102, 99.5, 101)
    bars[2] = bar(T0 + timedelta(minutes=2), 101, 104, 100, 103)
    row = run(bars, cfg, flat_by="10:02").trades[0]
    assert row["exit_reason"] == "flat_by"
    assert row["exit_px"] == 103.0
    assert row["r_multiple"] == pytest.approx(0.6)


# --- no lookahead ----------------------------------------------------------
def test_entry_fills_at_the_next_bar_open_not_the_signal_close(cfg):
    bars = signal_bars()
    bars[1]["open"] = 101.5
    row = run(bars, cfg).trades[0]
    assert row["entry_px"] == 101.5
    assert row["entry_ts"] == bars[1]["ts"].isoformat()


# --- costs -----------------------------------------------------------------
def test_costs_reduce_the_r_multiple(cfg):
    bars = signal_bars()
    bars[2] = bar(T0 + timedelta(minutes=2), 101, 112, 99, 111)
    gross = run(bars, cfg, costs=HarnessCosts(spread_bps=0.0)).trades[0]
    net = run(bars, cfg, costs=HarnessCosts(spread_bps=10.0)).trades[0]
    assert gross["r_multiple"] == 2.0
    # entry leg 10bps on 100*100 = 10; exit leg 10bps on 100*110 = 11; (1000-21)/500
    assert net["r_multiple"] == pytest.approx((1000.0 - 21.0) / 500.0)
    assert net["r_multiple"] < gross["r_multiple"]
    assert net["slippage_bps"] == pytest.approx(10.0)
    assert net["spread_bps"] == 10.0


# --- refusals --------------------------------------------------------------
def test_blocked_signal_is_skipped_with_its_binding_gate(cfg, mandate):
    """The real gate blocks TSLA (outside the mandate) => no trade, recorded."""
    bars = signal_bars()
    gate = make_real_gate(mandate)

    result = replay(
        bars_by_symbol={"TSLA": bars},
        starting_equity=100_000.0,
        config=cfg,
        gate_factory=gate,
        sizing_caps_factory=caps_factory,
        costs=HarnessCosts(spread_bps=0.0),
    )
    assert result.trades == ()
    blocked = [s for s in result.skipped if s["symbol"] == "TSLA"]
    assert blocked and blocked[0]["binding_gate"] == "mandate"
    assert blocked[0]["reason"].startswith("BLOCK:")


def test_symbol_with_too_few_bars_is_skipped_with_a_reason(cfg):
    result = replay(
        bars_by_symbol={"AAPL": [bar(T0, 100, 101, 99, 100, signal=True, stop=95)]},
        starting_equity=100_000.0,
        config=cfg,
        gate_factory=allow_gate,
        sizing_caps_factory=caps_factory,
        costs=HarnessCosts(spread_bps=0.0),
    )
    assert result.trades == ()
    assert any(
        s["symbol"] == "AAPL" and s["reason"] == "insufficient_bars" for s in result.skipped
    )


# --- the comparison unit ---------------------------------------------------
def test_trade_row_has_exactly_the_plan_keys():
    row = trade_row(
        sleeve="swing",
        symbol="MSFT",
        setup="ORB",
        entry_ts=T0,
        exit_ts=T0 + timedelta(minutes=5),
        qty=10,
        entry_px=100.0,
        exit_px=105.0,
        slippage_bps=1.0,
        fees_usd=0.5,
        r_multiple=1.0,
    )
    assert tuple(row) == TRADE_ROW_KEYS
    assert len(row) == 19


# --- scorecard -------------------------------------------------------------
def two_trades():
    return [
        trade_row(
            sleeve="swing",
            symbol="AAA",
            setup="ORB",
            entry_ts=datetime(2026, 1, 2, 10, 0),
            exit_ts=datetime(2026, 1, 2, 11, 0),
            qty=10,
            entry_px=100.0,
            exit_px=110.0,
            slippage_bps=0.0,
            fees_usd=0.0,
            r_multiple=2.0,
        ),
        trade_row(
            sleeve="swing",
            symbol="BBB",
            setup="ORB",
            entry_ts=datetime(2026, 1, 5, 10, 0),
            exit_ts=datetime(2026, 1, 5, 11, 0),
            qty=10,
            entry_px=100.0,
            exit_px=90.0,
            slippage_bps=0.0,
            fees_usd=0.0,
            r_multiple=-1.0,
        ),
    ]


def test_summarize_two_trades_hand_worked():
    """+100 then -100 USD from 100k; peak 100100, trough 100000.
    win_rate 1/2; PF 100/100; expectancy_R (2-1)/2; maxDD 100."""
    out = summarize(two_trades(), starting_equity=100_000.0)
    assert out["trades"] == 2
    assert out["net_pnl_usd"] == 0.0
    assert out["net_return"] == 0.0
    assert out["win_rate"] == 0.5
    assert out["profit_factor"] == 1.0
    assert out["expectancy_per_trade_r"] == 0.5
    assert out["max_drawdown_usd"] == 100.0
    assert out["max_drawdown_pct"] == pytest.approx(100.0 / 100_100.0)
    assert out["return_max_dd"] == 0.0
    assert out["avg_win_usd"] == 100.0
    assert out["avg_loss_usd"] == -100.0
    assert 0.0 <= out["exposure_pct"] <= 1.0


def test_summarize_empty_yields_none_metrics_with_reasons():
    out = summarize([], starting_equity=100_000.0)
    assert out["trades"] == 0
    assert out["ending_equity"] == 100_000.0
    for key, value in out.items():
        if key in {"trades", "starting_equity", "ending_equity"} or key.endswith("_reason"):
            continue
        assert value is None, f"{key} should be None with no trades, got {value!r}"
        assert out[f"{key}_reason"] == "no_trades"


# --- the real gate wiring --------------------------------------------------
def make_real_gate(mandate):
    def factory(context):
        return evaluate(
            GateContext(
                request=context.request,
                book=context.book,
                market=context.market,
                mandate=mandate,
                config=context.config,
                now=context.ts,
                ladder=rung_for(
                    day_pnl_pct=0.0,
                    five_day_pct=0.0,
                    drawdown_pct=0.0,
                    soft_pct=0.01,
                    hard_pct=0.03,
                    derisk_5d_pct=0.06,
                    derisk_dd_pct=0.10,
                    halt_dd_pct=0.15,
                ),
                vol=VolScalar(
                    scalar=1.0,
                    target=0.10,
                    realized=0.10,
                    warmup_ok=True,
                    applied=False,
                    reason="within_band",
                ),
                es=ESResult(
                    value_pct=0.004,
                    estimator="historical",
                    flagged=False,
                    window_days=500,
                    components={"historical": 0.004, "parametric": 0.003, "stress": 0.006},
                ),
                sleeve_ceiling_pct=0.70,
                sleeve_deployed_pct=0.0,
                setup_validated=True,
            )
        )

    return factory


@pytest.fixture()
def mandate(cfg, now):
    """The real gate needs a mandate; keep it valid far beyond the test clock."""
    raw = dict(DEFAULT_MANDATE)
    raw["created"] = now.date().isoformat()
    raw["expires"] = (now + timedelta(days=3650)).date().isoformat()
    write_mandate(cfg.mandate_path, raw)
    return load_mandate(cfg.mandate_path)
