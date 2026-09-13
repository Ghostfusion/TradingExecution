"""Tests for the trial registry, scorecard/comparator and allocation manager.

Observable contract only (see the ``signald.lineage`` module invariant): every
trial records the running N, the scorecard is net of each row's fees, a short
sample never declares a winner, and capital moves only on evidence that clears
the pre-registered noise bar.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from signald import lineage
from signald.lineage import (
    FEASIBILITY_CLAIM,
    TrialRegistry,
    compare,
    diversification,
    propose_allocation,
    scorecard,
    write_scorecard,
)
from signald.schema import sha256_of
from signald.stores import AuditChain

pytestmark = pytest.mark.timeout(120)


def _row(
    *,
    day: str,
    qty: float = 100.0,
    entry: float = 10.0,
    exit_px: float = 10.2,
    fees: float | None = 1.0,
    r_multiple: float | None = 2.0,
    slippage_bps: float | None = 1.0,
    sleeve: str = "swing",
) -> dict:
    """One plan §10.2 trade row with only the fields the tests exercise."""
    return {
        "sleeve": sleeve,
        "symbol": "AAA",
        "setup": "ORB",
        "entry_ts": f"{day}T09:40:00",
        "exit_ts": f"{day}T10:30:00",
        "qty": qty,
        "entry_px": entry,
        "exit_px": exit_px,
        "slippage_bps": slippage_bps,
        "fees_usd": fees,
        "r_multiple": r_multiple,
        "rvol_first5": 2.5,
        "adx_14": 26.0,
        "gap_pct": 0.5,
        "spread_bps": 3.0,
        "exit_reason": "target",
        "gate_verdict": "ALLOW",
        "binding_gate": None,
        "data_vintage": "sip",
    }


def _trade(day: str, net: float, *, sleeve: str = "swing") -> dict:
    """A trade whose net PnL (qty 1, zero fees) is exactly ``net``."""
    return _row(
        day=day,
        qty=1.0,
        entry=100.0,
        exit_px=100.0 + net,
        fees=0.0,
        r_multiple=1.0,
        slippage_bps=0.0,
        sleeve=sleeve,
    )


#: Hand-worked three-trade list: +20/-10/+50 gross, fees 1/1/2 -> net +19/-11/+48.
HAND = [
    _row(day="2026-01-05", exit_px=10.2, fees=1.0, r_multiple=2.0, slippage_bps=1.0),
    _row(day="2026-01-06", exit_px=9.9, fees=1.0, r_multiple=-1.0, slippage_bps=2.0),
    _row(day="2026-01-07", exit_px=10.5, fees=2.0, r_multiple=3.0, slippage_bps=3.0),
]


def test_registry_appends_rows_and_counts(tmp_path, now):
    path = tmp_path / "audit" / "trials.jsonl"
    reg = TrialRegistry(path, lambda: now)

    first = reg.register("orb-ma", notes="baseline", metric={"sharpe": 1.2})
    assert reg.count() == 1
    second = reg.register("orb-ma", notes="same variant, second trial")
    assert reg.count() == 2

    # A re-registered variant is a new trial with its own id; N counts trials.
    assert first.trial_id != second.trial_id
    assert (first.n_trials, second.n_trials) == (1, 2)
    assert first.started_at == now.isoformat(timespec="seconds")
    assert second.started_at == now.isoformat(timespec="seconds")

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 2  # appended, never overwritten
    rows = [json.loads(ln) for ln in lines]
    assert rows[0]["metric"] == {"sharpe": 1.2}
    assert rows[1]["variant"] == "orb-ma"
    # atomic append leaves no temp file behind
    assert list(path.parent.glob("*.tmp")) == []

    trials = reg.trials()
    assert [t.variant for t in trials] == ["orb-ma", "orb-ma"]
    assert [t.n_trials for t in trials] == [1, 2]
    assert trials[0].notes == "baseline"


def test_registry_writes_audit_row_and_rejects_non_numbers(tmp_path, now):
    audit = AuditChain(tmp_path / "audit.jsonl", lambda: now)
    reg = TrialRegistry(tmp_path / "trials.jsonl", lambda: now, audit=audit)

    reg.register("vwap-pullback")
    rows = audit.read()
    assert [r["kind"] for r in rows] == ["trial_registered"]
    assert rows[0]["data"]["n_trials"] == 1
    assert audit.verify()[0] is True

    with pytest.raises(ValueError):
        reg.register("bad", metric={"sharpe": float("nan")})
    assert reg.count() == 1  # failed trial never landed


def test_dsr_uses_registry_count_and_falls_with_n(tmp_path, now):
    calls = []

    def fake_dsr(*, sharpe, n_trials, observations):
        calls.append((sharpe, n_trials, observations))
        return 1.0 / n_trials

    reg = TrialRegistry(tmp_path / "trials.jsonl", lambda: now, dsrc=fake_dsr)
    values = []
    for variant in ("a", "b", "c"):
        reg.register(variant)
        values.append(reg.deflated_sharpe(sharpe=1.0, observations=250))

    assert [call[1] for call in calls] == [1, 2, 3]  # N = registry count
    assert calls[0] == (1.0, 1, 250)
    assert values[0] > values[1] > values[2]  # the deflation grows with N


def test_default_stats_resolution_wires_validation_stats():
    stats = pytest.importorskip("signald.validation.stats")
    assert lineage._resolve_dsr() is stats.deflated_sharpe
    assert lineage._resolve_paired() is stats.paired_comparison


def test_deflated_sharpe_threads_registry_n_into_real_stats(tmp_path, now):
    stats = pytest.importorskip("signald.validation.stats")
    reg = TrialRegistry(tmp_path / "trials.jsonl", lambda: now)

    reg.register("only")
    single = reg.deflated_sharpe(sharpe=0.05, observations=60)
    for index in range(4):
        reg.register(f"variant-{index}")
    many = reg.deflated_sharpe(sharpe=0.05, observations=60)

    assert reg.count() == 5
    assert single > many  # the deflation grows with the trial count
    # The registry passed N=5 to the one computation.
    assert many == pytest.approx(
        stats.deflated_sharpe(sharpe=0.05, n_trials=5, observations=60)
    )


def test_scorecard_hand_worked():
    card = scorecard(HAND, sleeve="swing", minimum_trades=30)

    assert card.sleeve == "swing"
    assert card.trades == 3
    assert card.sample_adequate is False  # below the pre-registered minimum
    assert card.reasons == {}  # every metric was computable

    m = card.metrics
    assert m["gross_pnl_usd"] == pytest.approx(60.0)
    assert m["fees_usd"] == pytest.approx(4.0)
    assert m["net_pnl_usd"] == pytest.approx(56.0)
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["expectancy_usd"] == pytest.approx(56 / 3)
    assert m["expectancy_r"] == pytest.approx(4 / 3)
    assert m["avg_win_usd"] == pytest.approx(33.5)
    assert m["avg_loss_usd"] == pytest.approx(11.0)
    assert m["payoff_ratio"] == pytest.approx(33.5 / 11.0)
    assert m["profit_factor"] == pytest.approx(67.0 / 11.0)
    assert m["max_drawdown_usd"] == pytest.approx(11.0)
    assert m["mean_slippage_bps"] == pytest.approx(2.0)

    assert scorecard(HAND, sleeve="swing", minimum_trades=3).sample_adequate is True


def test_scorecard_reports_unavailable_metrics_with_reasons():
    winners = [
        _row(day="2026-01-05", exit_px=10.2),
        _row(day="2026-01-06", exit_px=10.3),
    ]
    card = scorecard(winners, sleeve="swing")
    assert card.metrics["avg_loss_usd"] is None
    assert card.reasons["avg_loss_usd"] == "no losing trades"
    assert card.metrics["profit_factor"] is None
    assert "no losing trades" in card.reasons["profit_factor"]
    assert card.metrics["payoff_ratio"] is None
    assert "winning and losing" in card.reasons["payoff_ratio"]

    empty = scorecard([], sleeve="swing")
    assert empty.trades == 0
    assert empty.sample_adequate is False
    assert empty.metrics["net_pnl_usd"] is None
    assert set(empty.reasons) == set(empty.metrics)
    assert set(empty.reasons.values()) == {"no trades"}

    # A row whose fees are missing cannot produce a net figure: report it.
    broken = scorecard([_row(day="2026-01-05", fees=None)], sleeve="swing")
    assert broken.metrics["gross_pnl_usd"] == pytest.approx(20.0)
    assert broken.metrics["net_pnl_usd"] is None
    assert "fees_usd" in broken.reasons["net_pnl_usd"]


def test_compare_aligns_by_day_and_refuses_a_winner():
    a = [_trade("2026-01-05", 1.0), _trade("2026-01-06", 2.0)]
    b = [
        _trade("2026-01-05", -1.0, sleeve="intraday"),
        _trade("2026-01-08", 3.0, sleeve="intraday"),
    ]
    seen = {}

    def fake_paired(series_a, series_b):
        seen["a"] = list(series_a)
        seen["b"] = list(series_b)
        return {
            "mean_diff": 1.0,
            "ci_low": -2.0,
            "ci_high": 4.0,
            "share_positive": 0.6,
            "n": len(series_a),
            "excludes_zero": False,
        }

    out = compare(a, b, sleeve_a="swing", sleeve_b="intraday", paired=fake_paired)

    # Union of sessions, missing day filled with 0.0 (day-aligned, not row-aligned).
    assert seen["a"] == [1.0, 2.0, 0.0]
    assert seen["b"] == [-1.0, 0.0, 3.0]
    assert out["difference"]["sessions"] == 3
    assert out["difference"]["ci_low"] == pytest.approx(-2.0)
    assert out["difference"]["ci_high"] == pytest.approx(4.0)
    assert out["a"].trades == 2
    assert out["b"].trades == 2

    assert out["claim"] == FEASIBILITY_CLAIM
    assert "feasibility, not superiority" in out["claim"]
    assert "winner" in out["claim"]


def test_compare_uses_real_paired_comparison_by_default():
    pytest.importorskip("signald.validation.stats")
    start = datetime(2026, 1, 5)
    a = []
    b = []
    for offset in range(40):
        day = (start + timedelta(days=offset)).date().isoformat()
        a.append(_trade(day, 1.0 if offset % 2 else -0.5))
        b.append(_trade(day, 0.2 * offset - 3.0, sleeve="intraday"))

    out = compare(a, b, sleeve_a="swing", sleeve_b="intraday")
    diff = out["difference"]
    assert diff["sessions"] == 40
    assert diff["n"] == 40
    assert diff["mean_diff"] is not None
    assert diff["ci_low"] <= diff["mean_diff"] <= diff["ci_high"]
    assert diff["excludes_zero"] in (True, False)
    assert out["claim"] == FEASIBILITY_CLAIM  # 40 sessions is far below the bar

    again = compare(a, b, sleeve_a="swing", sleeve_b="intraday")
    assert again["difference"] == diff  # seeded, deterministic


def test_compare_reports_reason_when_a_side_is_empty():
    def forbidden(*args, **kwargs):
        raise AssertionError("paired comparison must not run without two sides")

    b = [
        _trade("2026-01-05", 1.0, sleeve="intraday"),
        _trade("2026-01-06", 2.0, sleeve="intraday"),
    ]
    out = compare([], b, sleeve_a="swing", sleeve_b="intraday", paired=forbidden)

    assert out["difference"]["reason"] == "one sleeve has no daily PnL to pair"
    assert out["difference"]["mean_diff"] is None
    assert out["difference"]["ci_low"] is None
    assert out["claim"] == FEASIBILITY_CLAIM


def test_diversification_correlation_identical_and_opposite():
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    base = [1.0, 2.0, -3.0, 4.0, -5.0]
    a = [_trade(day, value) for day, value in zip(days, base, strict=True)]
    same = [
        _trade(day, value, sleeve="intraday") for day, value in zip(days, base, strict=True)
    ]
    opposite = [
        _trade(day, -value, sleeve="intraday") for day, value in zip(days, base, strict=True)
    ]

    identical = diversification(a, same)
    assert identical["days"] == 5
    assert identical["correlation"] == pytest.approx(1.0)
    assert "correlation" not in identical["reasons"]

    mirrored = diversification(a, opposite)
    assert mirrored["correlation"] == pytest.approx(-1.0)
    assert mirrored["co_drawdown_incidence"] is not None
    # A 5% tail needs twenty sessions: honestly unavailable, with the reason.
    assert mirrored["tail_joint_share"] is None
    assert "20" in mirrored["reasons"]["tail_joint_share"]


def test_diversification_empty_side_is_unavailable_with_reason():
    one_sided = diversification([], [_trade("2026-01-05", 1.0)])
    assert one_sided["correlation"] is None
    assert one_sided["reasons"]["correlation"] == "fewer than two sessions"

    both_empty = diversification([], [])
    assert both_empty["days"] == 0
    assert both_empty["correlation"] is None
    assert both_empty["reasons"]["correlation"] == "no sessions to align"


def test_propose_allocation_inert_without_evidence(cfg):
    current = {"swing": 0.60, "intraday": 0.40}
    decisions = propose_allocation(current=current, evidence={}, config=cfg)

    assert [d.sleeve for d in decisions] == ["intraday", "swing"]  # deterministic order
    for decision in decisions:
        assert decision.proposed_pct == decision.current_pct
        assert decision.capped_by is None
        assert decision.frozen is False
        assert "noise bar" in decision.reason

    no_bar = {"swing": {"clears_noise_bar": False, "tilt_pct": 0.50}}
    swing = next(
        d for d in propose_allocation(current=current, evidence=no_bar, config=cfg)
        if d.sleeve == "swing"
    )
    assert swing.proposed_pct == pytest.approx(0.60)


def test_propose_allocation_honours_band_floor_ceiling(cfg):
    def decide(current_pct, tilt):
        return propose_allocation(
            current={"swing": current_pct},
            evidence={"swing": {"clears_noise_bar": True, "tilt_pct": tilt}},
            config=cfg,
        )[0]

    band = decide(0.40, 0.30)
    assert band.proposed_pct == pytest.approx(0.50)  # clamped to +band
    assert band.capped_by == "band"

    # cfg's swing capital ceiling (0.70) is the tighter of it and ceiling_pct.
    ceiling = decide(0.65, 0.20)
    assert ceiling.proposed_pct == pytest.approx(0.70)
    assert ceiling.capped_by == "ceiling"

    floor = decide(0.18, -0.20)
    assert floor.proposed_pct == pytest.approx(0.15)
    assert floor.capped_by == "floor"

    inside = decide(0.50, 0.05)
    assert inside.proposed_pct == pytest.approx(0.55)
    assert inside.capped_by is None

    no_tilt = decide(0.50, 0.0)
    assert no_tilt.proposed_pct == pytest.approx(0.50)
    assert "no tilt" in no_tilt.reason


def test_propose_allocation_refuses_when_frozen_or_in_drawdown(cfg):
    evidence = {"swing": {"clears_noise_bar": True, "tilt_pct": 0.05}}

    frozen = propose_allocation(
        current={"swing": 0.50}, evidence=evidence, config=cfg, frozen=True
    )[0]
    assert frozen.frozen is True
    assert frozen.proposed_pct == pytest.approx(0.50)
    assert "frozen" in frozen.reason

    in_drawdown = propose_allocation(
        current={"swing": 0.50},
        evidence={"swing": {**evidence["swing"], "in_drawdown": True}},
        config=cfg,
    )[0]
    assert in_drawdown.proposed_pct == pytest.approx(0.50)
    assert "drawdown" in in_drawdown.reason

    hot = propose_allocation(
        current={"swing": 0.50},
        evidence={"swing": {**evidence["swing"], "hot_streak": True}},
        config=cfg,
    )[0]
    assert hot.proposed_pct == pytest.approx(0.50)
    assert "hot-streak" in hot.reason


def test_write_scorecard_writes_json_and_markdown_naming_n(tmp_path, now):
    card = scorecard(HAND, sleeve="swing", minimum_trades=30)
    comparison = compare(
        HAND,
        [_trade("2026-01-05", -2.0, sleeve="intraday")],
        sleeve_a="swing",
        sleeve_b="intraday",
        paired=lambda a, b: {
            "mean_diff": 1.0,
            "ci_low": -1.0,
            "ci_high": 3.0,
            "share_positive": 0.5,
            "n": len(a),
            "excludes_zero": False,
        },
    )
    out = write_scorecard(
        tmp_path / "signals" / "scorecard" / "2026-09-11",
        scorecards={"swing": card},
        comparison=comparison,
        trials=7,
        generated_at=now,
    )

    md_path = tmp_path / "signals" / "scorecard" / "2026-09-11.md"
    json_path = tmp_path / "signals" / "scorecard" / "2026-09-11.json"
    assert out == md_path
    assert json_path.exists()
    assert md_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["trials"] == 7
    assert payload["generated_at"] == now.isoformat(timespec="seconds")
    assert "feasibility, not superiority" in payload["claim_limit"]
    assert payload["scorecards"]["swing"]["trades"] == 3
    assert payload["comparison"]["difference"]["sessions"] == 3
    body = {k: v for k, v in payload.items() if k != "artifact_sha256"}
    assert payload["artifact_sha256"] == sha256_of(body)

    markdown = md_path.read_text(encoding="utf-8")
    assert "Trials (N): 7" in markdown
    assert "feasibility, not superiority" in markdown
    assert "insufficient evidence" in markdown
