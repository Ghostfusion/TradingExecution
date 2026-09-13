"""Guarded-live scaffolding tests (plan P5, §11.3/§11.4).

The invariant under test is fail-closed: the order path is unreachable without
the opt-ins, and a promotion is complete only when every checklist item is
machine-satisfied.  Pure functions - no clock, no I/O, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signald.promotion import (
    MIN_COST_MODEL_FILLS,
    PromotionReport,
    hard_cap_violation,
    opt_ins,
    promotion_report,
)

pytestmark = pytest.mark.timeout(120)

REQUIRED_DRILLS = tuple(f"D{i}" for i in range(1, 13))
PASSING_VALIDATION = {
    "ORB_RVOL": {"dsr": 0.96, "pbo": 0.04, "trades": 80, "oos_months": 8.0, "n": 12},
}


def _report(**overrides) -> PromotionReport:
    """A promotion report with every live-profile item satisfied, then overridden."""
    kwargs = {
        "profile": "live",
        "sessions_reconciled": 20,
        "drills_passed": list(REQUIRED_DRILLS),
        "required_drills": list(REQUIRED_DRILLS),
        "validation": PASSING_VALIDATION,
        "kill_switch_tested": True,
        "daily_loss_tested": True,
        "stale_data_tested": True,
        "hard_cap_usd": 25_000.0,
        "approval_threshold_usd": 5_000.0,
        "backups_tested": True,
        "owner_signed_date": "2026-09-12",
    }
    kwargs.update(overrides)
    return promotion_report(**kwargs)


# --------------------------------------------------------------------------
# opt_ins: the full mode x execute x ack truth table
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mode", "execute_flag", "live_ack", "allowed", "reason"),
    [
        ("signal", False, False, False, "signal has no order path"),
        ("signal", True, False, False, "signal has no order path"),
        ("signal", True, True, False, "signal has no order path"),
        ("paper", False, False, False, "paper requires --execute"),
        ("paper", False, True, False, "paper requires --execute"),
        ("paper", True, False, True, ""),
        ("paper", True, True, True, ""),
        ("live", False, False, False, "live requires --execute"),
        ("live", False, True, False, "live requires --execute"),
        ("live", True, False, False, "live requires the acknowledgement flag"),
        ("live", True, True, True, ""),
        ("bogus", True, True, False, "unknown mode 'bogus'"),
    ],
)
def test_opt_ins_truth_table(mode, execute_flag, live_ack, allowed, reason):
    assert opt_ins(mode=mode, execute_flag=execute_flag, live_ack=live_ack) == (allowed, reason)


# --------------------------------------------------------------------------
# promotion_report: checklist completeness
# --------------------------------------------------------------------------
def test_all_items_satisfied_is_complete():
    r = _report()
    assert r.complete is True
    assert r.blockers == ()
    assert r.profile == "live"
    assert [item.id for item in r.items] == [
        "paper_sessions",
        "drills",
        "validation_gates",
        "cost_model",
        "kill_switch",
        "daily_loss_flatten",
        "stale_data_block",
        "ops_readiness",
        "owner_signoff",
        "hard_cap",
        "approval_thresholds",
        "opt_ins",
    ]


def test_one_missing_drill_is_incomplete_and_named():
    r = _report(drills_passed=[d for d in REQUIRED_DRILLS if d != "D7"])
    assert r.complete is False
    assert "drills" in r.blockers
    item = next(i for i in r.items if i.id == "drills")
    assert item.satisfied is False
    assert "D7" in item.detail


def test_live_profile_without_hard_cap_is_blocked():
    r = _report(hard_cap_usd=None)
    assert r.complete is False
    assert "hard_cap" in r.blockers
    # the same report is promotable for paper, where the cap is not required
    paper = _report(profile="paper", hard_cap_usd=None, approval_threshold_usd=None)
    assert paper.complete is True
    assert "hard_cap" not in paper.blockers


def test_unpassed_validation_gate_blocks():
    r = _report(
        validation={
            "ORB_RVOL": {"dsr": 0.96, "pbo": 0.20, "trades": 80, "oos_months": 8.0, "n": 12}
        }
    )
    assert r.complete is False
    assert "validation_gates" in r.blockers
    detail = next(i for i in r.items if i.id == "validation_gates").detail
    assert "ORB_RVOL" in detail and "PBO" in detail


def test_no_validation_evidence_blocks():
    r = _report(validation=None)
    assert r.complete is False
    assert "validation_gates" in r.blockers
    assert "no setup validation evidence" in next(
        i for i in r.items if i.id == "validation_gates"
    ).detail


def test_validation_gate_boundaries_are_hand_worked():
    # exactly at every threshold passes (DSR 0.95, PBO 0.05, 50 trades, 6 months, N >= 1)
    at = _report(
        validation={
            "GAP_GO": {"dsr": 0.95, "pbo": 0.05, "trades": 50, "oos_months": 6.0, "n": 1}
        }
    )
    assert "validation_gates" not in at.blockers
    # one basis point worse on DSR, and one month short on OOS, each fail
    below = _report(
        validation={
            "GAP_GO": {"dsr": 0.9499, "pbo": 0.05, "trades": 50, "oos_months": 5.9, "n": 1}
        }
    )
    assert "validation_gates" in below.blockers
    detail = next(i for i in below.items if i.id == "validation_gates").detail
    assert "DSR" in detail and "OOS" in detail
    # a record without N cannot be promoted (DSR needs the trial count)
    no_n = _report(
        validation={"GAP_GO": {"dsr": 0.99, "pbo": 0.01, "trades": 90, "oos_months": 9.0}}
    )
    assert "validation_gates" in no_n.blockers


def test_cost_model_fill_boundary():
    # the cost model is scored on the realised paper fills recorded per setup
    at = _report(
        validation={
            "GAP_GO": {
                "dsr": 0.99,
                "pbo": 0.01,
                "trades": MIN_COST_MODEL_FILLS,
                "oos_months": 9.0,
                "n": 3,
            }
        }
    )
    assert "cost_model" not in at.blockers
    below = _report(
        validation={
            "GAP_GO": {
                "dsr": 0.99,
                "pbo": 0.01,
                "trades": MIN_COST_MODEL_FILLS - 1,
                "oos_months": 9.0,
                "n": 3,
            }
        }
    )
    assert "cost_model" in below.blockers


def test_blockers_are_in_stable_checklist_order():
    r = _report(
        drills_passed=[],
        validation=None,
        kill_switch_tested=False,
        daily_loss_tested=False,
        stale_data_tested=False,
        backups_tested=False,
        owner_signed_date=None,
        sessions_reconciled=0,
        hard_cap_usd=None,
        approval_threshold_usd=None,
    )
    assert r.blockers == (
        "paper_sessions",
        "drills",
        "validation_gates",
        "cost_model",
        "kill_switch",
        "daily_loss_flatten",
        "stale_data_block",
        "ops_readiness",
        "owner_signoff",
        "hard_cap",
        "approval_thresholds",
    )
    assert r.complete is False


def test_unknown_profile_is_refused():
    with pytest.raises(ValueError):
        _report(profile="turbo")


def test_opt_ins_item_proves_both_opt_ins_for_live():
    live = _report()
    opt_item = next(i for i in live.items if i.id == "opt_ins")
    assert opt_item.satisfied is True
    assert "opt_ins" not in _report(profile="paper").blockers


# --------------------------------------------------------------------------
# hard_cap_violation
# --------------------------------------------------------------------------
def test_hard_cap_violation_over_and_under():
    over = hard_cap_violation(notional_usd=30_000.0, hard_cap_usd=25_000.0)
    assert over is not None and "exceeds hard cap" in over
    assert hard_cap_violation(notional_usd=25_000.0, hard_cap_usd=25_000.0) is None
    assert hard_cap_violation(notional_usd=10_000.0, hard_cap_usd=25_000.0) is None


def test_hard_cap_violation_without_a_cap_is_none():
    # a missing cap is not a violation here; the live report carries that blocker
    assert hard_cap_violation(notional_usd=10_000_000.0, hard_cap_usd=None) is None


def test_hard_cap_violation_fails_closed_on_unusable_inputs():
    assert hard_cap_violation(notional_usd=1.0, hard_cap_usd=0.0) is not None
    assert hard_cap_violation(notional_usd=float("nan"), hard_cap_usd=25_000.0) is not None


# --------------------------------------------------------------------------
# RUNBOOK.md: sections cannot silently disappear
# --------------------------------------------------------------------------
def test_runbook_has_required_sections():
    text = (Path(__file__).resolve().parents[1] / "docs" / "RUNBOOK.md").read_text(
        encoding="utf-8"
    )
    for heading in (
        "## Daily lifecycle",
        "## Incident runbook",
        "## Kill switch and re-arm",
        "## Promotion checklist",
    ):
        assert heading in text, f"RUNBOOK.md lost section {heading!r}"


def test_runbook_alert_table_covers_every_alert():
    text = (Path(__file__).resolve().parents[1] / "docs" / "RUNBOOK.md").read_text(
        encoding="utf-8"
    )
    for alert in (
        "data_stale",
        "spread_blowout",
        "halt_entered",
        "duplicate_client_order_id",
        "protective_stop_missing",
        "reconcile_drift",
        "daily_loss_soft",
        "daily_loss_hard",
        "derisk_rung",
        "kill_switch",
        "clock_offset",
        "broker_error_storm",
        "gate_exception",
        "sleeve_ceiling_hit",
        "flat_verification_failed",
        "heartbeat_loss",
    ):
        assert f"`{alert}`" in text, f"RUNBOOK.md lost alert {alert!r}"


def test_runbook_daily_table_and_rearm_steps():
    text = (Path(__file__).resolve().parents[1] / "docs" / "RUNBOOK.md").read_text(
        encoding="utf-8"
    )
    for moment in ("08:00", "09:00", "09:35", "11:00", "15:45", "16:15", "17:00"):
        assert moment in text, f"RUNBOOK.md lost lifecycle time {moment}"
    assert "25%" in text and "50%" in text and "100%" in text  # staged size restore
    assert "post-mortem" in text.lower()
    assert "RTO" in text and "RPO" in text
