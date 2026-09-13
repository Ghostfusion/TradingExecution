"""Path-independent de-risking ladder (plan §9, design §7.5).

The ladder is a pure function of the *equity path*, never of a losing streak:
streak-based circuit breakers are explicitly rejected (design §7.5) because
under any fixed edge a losing run is expected, and cutting sample size after a
random run converts a bad day into a bad month. Recovery takes ~3x the time a
drawdown took to build (Triple Penance), so rungs de-risk early and re-risk
slowly.

Five rungs; thresholds are positive fractions of house equity; PnL inputs are
signed (a loss is negative); comparison is ``<=`` so an exactly-hit threshold
trips:

===== ================================== ===========================================
 rung  trigger                            action
===== ================================== ===========================================
  1    ``day <= -soft``                   no new intraday entries (swing unaffected)
  2    ``day <= -hard``                   flatten intraday, no new intraday, swing x0.5
  3    ``5d <= -derisk_5d`` or             both sleeves x0.5, allocation frozen
       ``dd <= -derisk_dd``
  4    ``dd <= -halt_dd``                 full halt, flatten, freeze, manual re-arm
===== ================================== ===========================================

Worst applicable rung wins (4 > 3 > 2 > 1); the result is the *action*, not a
suggestion - the gate, the sizer and the order path obey it verbatim.

Fail closed: a non-finite PnL input (unavailable or garbage state) or a
non-positive threshold cannot be evaluated faithfully, so it is rejected with
``ValueError`` rather than silently passing; the caller's gate turns that into
a ``BLOCK``, never a pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: The closed set of rungs, most-permissive first. `rung` is always a member.
LADDER_RUNGS: tuple[int, ...] = (0, 1, 2, 3, 4)

_REASON_NONE = "none"
_REASON_RUNG1 = "rung1_soft_day"
_REASON_RUNG2 = "rung2_hard_day"
_REASON_RUNG3 = "rung3_derisk"
_REASON_RUNG4 = "rung4_halt"


@dataclass(frozen=True)
class LadderState:
    """The single rung's action: what the book may do until the path recovers."""

    rung: int  # 0 = none; 4 = full stop
    size_multiplier: float  # house-wide scale the sizer applies: 1.0 | 0.5 | 0.0
    allow_new_intraday: bool
    flatten_intraday: bool
    freeze_allocation: bool
    halt: bool  # full stop; re-armed manually, never on the next session's clock
    reason: str  # stable machine-readable code for the alert/audit payload


# One frozen instance per rung: `rung_for` returns these directly (no per-call
# allocation) and callers may compare by identity.
_RUNG_STATES: dict[int, LadderState] = {
    0: LadderState(0, 1.0, True, False, False, False, _REASON_NONE),
    1: LadderState(1, 1.0, False, False, False, False, _REASON_RUNG1),
    2: LadderState(2, 0.5, False, True, False, False, _REASON_RUNG2),
    3: LadderState(3, 0.5, False, False, True, False, _REASON_RUNG3),
    4: LadderState(4, 0.0, False, True, True, True, _REASON_RUNG4),
}


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")


def _require_positive(name: str, value: float) -> None:
    if not (math.isfinite(value) and value > 0.0):
        raise ValueError(f"{name} must be a positive fraction, got {value!r}")


def rung_for(
    *,
    day_pnl_pct: float,
    five_day_pct: float,
    drawdown_pct: float,
    soft_pct: float,
    hard_pct: float,
    derisk_5d_pct: float,
    derisk_dd_pct: float,
    halt_dd_pct: float,
) -> LadderState:
    """Return the worst applicable rung's action (design §7.5).

    `day_pnl_pct` is today's return, `five_day_pct` the trailing 5-session
    return and `drawdown_pct` the peak-to-trough drawdown, all as signed
    fractions of house equity. Thresholds are positive fractions; the worst
    rung wins (4 beats 3 beats 2 beats 1).
    """
    _require_finite("day_pnl_pct", day_pnl_pct)
    _require_finite("five_day_pct", five_day_pct)
    _require_finite("drawdown_pct", drawdown_pct)
    _require_positive("soft_pct", soft_pct)
    _require_positive("hard_pct", hard_pct)
    _require_positive("derisk_5d_pct", derisk_5d_pct)
    _require_positive("derisk_dd_pct", derisk_dd_pct)
    _require_positive("halt_dd_pct", halt_dd_pct)

    if drawdown_pct <= -halt_dd_pct:
        return _RUNG_STATES[4]
    if five_day_pct <= -derisk_5d_pct or drawdown_pct <= -derisk_dd_pct:
        return _RUNG_STATES[3]
    if day_pnl_pct <= -hard_pct:
        return _RUNG_STATES[2]
    if day_pnl_pct <= -soft_pct:
        return _RUNG_STATES[1]
    return _RUNG_STATES[0]


def describe(state: LadderState) -> str:
    """One human line for the alert/audit payload (used by the notifier).

    Derived from the state's fields so it can never disagree with the action
    the gate is about to enforce.
    """
    parts = [f"rung {state.rung} ({state.reason})", f"size x{state.size_multiplier:.2f}"]
    if state.halt:
        parts.append("HALT (manual re-arm required)")
    if state.flatten_intraday:
        parts.append("flatten intraday")
    if not state.allow_new_intraday:
        parts.append("no new intraday entries")
    if state.freeze_allocation:
        parts.append("allocation frozen")
    return "; ".join(parts)
