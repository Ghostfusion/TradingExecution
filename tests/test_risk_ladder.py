"""De-risking ladder unit tests (plan §9, design §7.5).

Every rung, the worst-rung precedence, the inclusive boundary, monotone
positive-day behaviour and the fail-closed rejection of an unevaluable state.
Thresholds mirror the config defaults (`signald/config.py`).
"""

from __future__ import annotations

import math

import pytest

from signald.risk.ladder import LADDER_RUNGS, LadderState, describe, rung_for

pytestmark = pytest.mark.timeout(120)

# Config defaults (plan §3): positive fractions of house equity.
SOFT = 0.01
HARD = 0.03
DERISK_5D = 0.06
DERISK_DD = 0.10
HALT_DD = 0.15


def _rung(*, day: float = 0.0, five_day: float = 0.0, drawdown: float = 0.0) -> LadderState:
    """Call the ladder with the house config defaults."""
    return rung_for(
        day_pnl_pct=day,
        five_day_pct=five_day,
        drawdown_pct=drawdown,
        soft_pct=SOFT,
        hard_pct=HARD,
        derisk_5d_pct=DERISK_5D,
        derisk_dd_pct=DERISK_DD,
        halt_dd_pct=HALT_DD,
    )


def test_ladder_rungs_is_the_closed_set():
    assert LADDER_RUNGS == (0, 1, 2, 3, 4)


def test_rung_zero_when_nothing_is_breached():
    state = _rung()
    assert state.rung == 0
    assert state.size_multiplier == 1.0
    assert state.allow_new_intraday is True
    assert state.flatten_intraday is False
    assert state.freeze_allocation is False
    assert state.halt is False
    assert state.reason == "none"


def test_rung_one_pauses_new_intraday_without_resizing():
    state = _rung(day=-SOFT)
    assert state.rung == 1
    assert state.reason == "rung1_soft_day"
    assert state.allow_new_intraday is False
    assert state.size_multiplier == 1.0
    assert state.flatten_intraday is False
    assert state.freeze_allocation is False
    assert state.halt is False


def test_rung_two_flattens_intraday_but_swing_trades_at_half():
    state = _rung(day=-HARD)
    assert state.rung == 2
    assert state.reason == "rung2_hard_day"
    assert state.flatten_intraday is True
    assert state.allow_new_intraday is False
    # swing is not flattened: it keeps trading, scaled to half size.
    assert state.size_multiplier == 0.5
    assert state.freeze_allocation is False
    assert state.halt is False


def test_rung_three_derisks_both_sleeves_and_freezes_allocation_without_halting():
    state = _rung(five_day=-DERISK_5D)
    assert state.rung == 3
    assert state.reason == "rung3_derisk"
    assert state.size_multiplier == 0.5
    assert state.freeze_allocation is True
    assert state.halt is False


def test_rung_three_also_fires_on_peak_to_trough_drawdown():
    state = _rung(drawdown=-DERISK_DD)
    assert state.rung == 3
    assert state.reason == "rung3_derisk"


def test_rung_four_halts_and_requires_manual_rearm():
    state = _rung(drawdown=-HALT_DD)
    assert state.rung == 4
    assert state.reason == "rung4_halt"
    assert state.size_multiplier == 0.0
    assert state.halt is True
    assert state.flatten_intraday is True
    assert state.freeze_allocation is True
    assert state.allow_new_intraday is False


@pytest.mark.parametrize(
    ("kwargs", "expected_rung"),
    [
        # An exactly-hit threshold trips (`<=`).
        ({"day": -SOFT}, 1),
        ({"day": -HARD}, 2),
        ({"five_day": -DERISK_5D}, 3),
        ({"drawdown": -DERISK_DD}, 3),
        ({"drawdown": -HALT_DD}, 4),
        # One tick short of the threshold: the rung below it.
        ({"day": -SOFT + 0.0001}, 0),
        ({"day": -HARD + 0.0001}, 1),
        ({"five_day": -DERISK_5D + 0.0001}, 0),
        ({"drawdown": -DERISK_DD + 0.0001}, 0),
        ({"drawdown": -HALT_DD + 0.0001}, 3),
    ],
)
def test_boundary_is_inclusive(kwargs, expected_rung):
    assert _rung(**kwargs).rung == expected_rung


@pytest.mark.parametrize("day", [0.0, 0.005, SOFT, HARD, HALT_DD, 0.5])
def test_positive_day_never_trips_a_rung(day):
    state = _rung(day=day, five_day=day, drawdown=day)
    assert state.rung == 0
    assert state.reason == "none"


@pytest.mark.parametrize(
    ("kwargs", "expected_rung"),
    [
        # All four rungs at once: the halt wins over every lower rung.
        ({"day": -HARD, "five_day": -DERISK_5D, "drawdown": -HALT_DD}, 4),
        # Rungs 1,2,3 at once: rung 3 wins, not the milder rung 2.
        ({"day": -HARD, "five_day": -DERISK_5D, "drawdown": -DERISK_DD}, 3),
        # Rungs 1 and 2 at once: rung 2 wins.
        ({"day": -HARD}, 2),
        # Rung 3 and rung 1 at once: rung 3 wins.
        ({"day": -SOFT, "drawdown": -DERISK_DD}, 3),
    ],
)
def test_worst_rung_wins_when_several_trigger(kwargs, expected_rung):
    assert _rung(**kwargs).rung == expected_rung


@pytest.mark.parametrize(
    ("kwargs", "expected_multiplier"),
    [
        ({"day": 0.0}, 1.0),
        ({"day": -SOFT}, 1.0),
        ({"day": -HARD}, 0.5),
        ({"five_day": -DERISK_5D}, 0.5),
        ({"drawdown": -DERISK_DD}, 0.5),
        ({"drawdown": -HALT_DD}, 0.0),
    ],
)
def test_size_multiplier_is_exactly_the_sizer_scale(kwargs, expected_multiplier):
    # The sizer's only job with this number is to multiply: prove the product is
    # the intended scaled size, not merely close to it.
    base_size = 200.0
    scaled = base_size * _rung(**kwargs).size_multiplier
    assert scaled == base_size * expected_multiplier


def test_multipliers_never_increase_with_severity():
    multipliers = [_rung(**kw).size_multiplier for kw in (
        {},
        {"day": -SOFT},
        {"day": -HARD},
        {"five_day": -DERISK_5D},
        {"drawdown": -HALT_DD},
    )]
    assert multipliers == [1.0, 1.0, 0.5, 0.5, 0.0]
    assert all(b <= a for a, b in zip(multipliers, multipliers[1:], strict=False))


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({}, "rung 0 (none)"),
        ({"day": -SOFT}, "rung 1 (rung1_soft_day)"),
        ({"day": -HARD}, "flatten intraday"),
        ({"five_day": -DERISK_5D}, "allocation frozen"),
        ({"drawdown": -HALT_DD}, "manual re-arm required"),
    ],
)
def test_describe_is_a_single_line_naming_the_action(kwargs, needle):
    text = describe(_rung(**kwargs))
    assert needle in text
    assert "\n" not in text


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("field", ["day", "five_day", "drawdown"])
def test_non_finite_pnl_is_rejected_fail_closed(field, bad):
    kwargs = {"day": 0.0, "five_day": 0.0, "drawdown": 0.0}
    kwargs[field] = bad
    with pytest.raises(ValueError):
        _rung(**kwargs)


@pytest.mark.parametrize("field", ["soft", "hard", "derisk_5d", "derisk_dd", "halt_dd"])
@pytest.mark.parametrize("bad", [0.0, -0.01, math.nan])
def test_non_positive_threshold_is_rejected(field, bad):
    thresholds = {
        "soft_pct": SOFT,
        "hard_pct": HARD,
        "derisk_5d_pct": DERISK_5D,
        "derisk_dd_pct": DERISK_DD,
        "halt_dd_pct": HALT_DD,
    }
    field_names = {
        "soft": "soft_pct",
        "hard": "hard_pct",
        "derisk_5d": "derisk_5d_pct",
        "derisk_dd": "derisk_dd_pct",
        "halt_dd": "halt_dd_pct",
    }
    thresholds[field_names[field]] = bad
    with pytest.raises(ValueError):
        rung_for(day_pnl_pct=0.0, five_day_pct=0.0, drawdown_pct=0.0, **thresholds)
