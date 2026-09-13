"""The single sizer (plan §4.3, design §7.2) - the only place a quantity is produced.

A sleeve submits a :class:`~signald.risk.state.RiskRequest` - risk in R, never a
quantity - and :func:`size` turns it into an integer share count through one
fixed pipeline:

    risk budget (request / sleeve remaining / house heat remaining)
      -> volatility scalar
      -> fractional-Kelly cap (validated edge, shrunk)
      -> liquidity participation cap
      -> sleeve notional room
      -> floor (+ the leverage cap when configured)

Invariants:

* **Fail closed.** A missing or unevaluable input produces ``ok=False``,
  ``qty=0`` and a stable reason code; it never falls back to a placeholder and
  never assumes a value. An unvalidated setup (``SizingCaps.validated_edge is
  None``) sizes to zero, because the plan's Kelly input for it is zero.
* **Never round up, never widen.** The quantity is ``floor`` of the tightest
  candidate; a stop is never widened so that a size becomes affordable.
* **One computation.** The arithmetic lives here and nowhere else; callers
  supply caps and do not re-derive them.

``RiskDecision.qty_risk`` / ``qty_liquidity`` / ``qty_notional`` are the
pre-floor candidates for the audit trail. On any rejection ``risk_pct`` is 0.0
(no risk is deployed) and ``binding`` names the cap that drove the quantity
below one share, or is ``None`` when the pipeline stopped before the floor
(invalid or missing input, not a cap comparison).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .state import RiskRequest


@dataclass(frozen=True)
class SizingCaps:
    """The sleeve-supplied knobs for one sizing call (plan §4.3, design §7.2).

    Every field is a cap or an input, never a recomputed quantity.
    """

    sleeve_risk_remaining_pct: float  # of house equity
    house_heat_remaining_pct: float  # of house equity
    sleeve_notional_room_usd: float
    participation_cap_pct: float  # of trailing volume, e.g. 0.25 of ADV
    kelly_fraction: float
    validated_edge: float | None = None  # per-unit edge from the validation record
    kelly_win_loss_ratio: float | None = None  # g/l; None -> 1.0
    kelly_shrink: float = 1.0  # estimation-error haircut in (0, 1]
    max_leverage_pct: float | None = None  # cap on |notional| / equity


@dataclass(frozen=True)
class RiskDecision:
    """The sizer's verdict: a quantity or a fail-closed rejection (plan §4.3)."""

    ok: bool
    qty: int
    risk_pct: float  # the risk fraction actually used (0.0 on any rejection)
    binding: str | None  # "risk" | "sleeve" | "heat" | "vol" | "kelly" |
    #                      "liquidity" | "notional" | "leverage"
    reason: str  # "ok" | "size_below_one_share" | "unvalidated_setup" |
    #              "no_stop_distance" | "no_price"
    qty_risk: float  # the pre-floor candidates, for the audit trail
    qty_liquidity: float
    qty_notional: float


def _is_number(value: object) -> bool:
    """True only for a finite real number; anything else cannot be evaluated."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value: object) -> bool:
    """True only for a finite number strictly above zero (NaN fails closed)."""
    return _is_number(value) and value > 0.0


def _finite_or_zero(value: float | None) -> float:
    """A cap or datum that is not a finite number cannot be evaluated -> 0.0.

    Zero is the conservative reading: it can only shrink a candidate, so an
    unusable input forces a rejection instead of letting an unknown through.
    """
    return float(value) if _is_number(value) else 0.0


def _reject(
    reason: str,
    *,
    binding: str | None = None,
    qty_risk: float = 0.0,
    qty_liquidity: float = 0.0,
    qty_notional: float = 0.0,
) -> RiskDecision:
    """A fail-closed verdict: no quantity, no risk, plus any computed candidates."""
    return RiskDecision(
        ok=False,
        qty=0,
        risk_pct=0.0,
        binding=binding,
        reason=reason,
        qty_risk=qty_risk,
        qty_liquidity=qty_liquidity,
        qty_notional=qty_notional,
    )


def kelly_cap(
    *,
    edge: float,
    fraction: float,
    win_loss_ratio: float = 1.0,
    shrink: float = 1.0,
) -> float:
    """Fractional Kelly, expressed only through the win/loss ratio (plan §4.4).

    The validation record carries an edge per unit risked - ``A = p*R - (1-p)``
    with ``R = g/l`` (plan §4.2 formula 8) - so the win probability is
    ``p = (1 + edge) / (1 + R)`` and the full-Kelly fraction is
    ``f* = (R*p - q) / R`` with ``q = 1 - p``. The used fraction is
    ``fraction * f* * shrink``, floored at zero: a non-positive edge is no bet.

    Anything that cannot be evaluated (missing, non-finite, or non-positive
    fraction/shrink/ratio) returns 0.0 - no validated input, no size.
    """
    if not (_is_number(edge) and _is_number(fraction) and _is_number(shrink)):
        return 0.0
    if fraction <= 0.0 or shrink <= 0.0:
        return 0.0
    ratio = 1.0 if win_loss_ratio is None else win_loss_ratio
    if not _is_number(ratio) or ratio <= 0.0:
        return 0.0
    win_prob = (1.0 + edge) / (1.0 + ratio)
    full_kelly = (ratio * win_prob - (1.0 - win_prob)) / ratio
    return max(0.0, fraction * full_kelly * shrink)


def size(req: RiskRequest, *, caps: SizingCaps, vol_scalar: float = 1.0) -> RiskDecision:
    """Turn one risk request into an integer quantity, or reject (plan §4.3).

    The pipeline order is fixed; the first step that cannot be honoured decides
    the reason and no later cap can loosen it.
    """
    # 1. A protective stop is the precondition of a risk-based size. No stop,
    #    no trade - and the stop is never widened to make a size fit.
    if not _positive(req.stop_distance):
        return _reject("no_stop_distance")

    # 2. The Kelly edge must come from the setup's pre-registered validation
    #    record. An unvalidated setup's Kelly input is zero, so its size is zero.
    if caps.validated_edge is None:
        return _reject("unvalidated_setup")

    # 3. The tightest of the three risk budgets (a tie keeps the earlier name).
    budgets = (
        ("risk", _finite_or_zero(req.requested_risk_pct)),
        ("sleeve", _finite_or_zero(caps.sleeve_risk_remaining_pct)),
        ("heat", _finite_or_zero(caps.house_heat_remaining_pct)),
    )
    binding, risk_pct = min(budgets, key=lambda item: item[1])

    # 4. Volatility scaling. A scalar at or below zero is unusable, not a zero.
    if not _positive(vol_scalar):
        return _reject("size_below_one_share")
    risk_pct = max(0.0, risk_pct * vol_scalar)
    if vol_scalar < 1.0:
        binding = "vol"  # the scalar, not a budget, is what shrank the risk

    # 5. Fractional Kelly is an input, never a standalone sizer.
    kelly = kelly_cap(
        edge=caps.validated_edge,
        fraction=caps.kelly_fraction,
        win_loss_ratio=(
            caps.kelly_win_loss_ratio if caps.kelly_win_loss_ratio is not None else 1.0
        ),
        shrink=caps.kelly_shrink,
    )
    if kelly < risk_pct:
        risk_pct = kelly
        binding = "kelly"

    # 6-7. The risk and liquidity candidates, from the capped risk fraction.
    equity = _finite_or_zero(req.equity)
    qty_risk = equity * risk_pct / req.stop_distance
    qty_liquidity = _finite_or_zero(caps.participation_cap_pct) * _finite_or_zero(
        req.trailing_volume
    )

    # 8. The sleeve's remaining notional room; a bad price cannot be evaluated.
    if not _positive(req.price):
        return _reject("no_price", qty_risk=qty_risk, qty_liquidity=qty_liquidity)
    qty_notional = _finite_or_zero(caps.sleeve_notional_room_usd) / req.notional_per_share

    # 9. The tightest candidate wins (a tie keeps the earlier name); the
    #    leverage ceiling then floors the whole thing when it is configured.
    qty_raw, binding = min(
        ((qty_risk, binding), (qty_liquidity, "liquidity"), (qty_notional, "notional")),
        key=lambda item: item[0],
    )
    if caps.max_leverage_pct is not None:
        leverage_qty = equity * _finite_or_zero(caps.max_leverage_pct) / req.notional_per_share
        if leverage_qty < qty_raw:
            qty_raw = leverage_qty
            binding = "leverage"

    # 10. Floor, never round up; below one share is a rejection, not a round-up.
    if not math.isfinite(qty_raw):
        return _reject("size_below_one_share")
    qty = math.floor(qty_raw)
    if qty < 1:
        return _reject(
            "size_below_one_share",
            binding=binding,
            qty_risk=qty_risk,
            qty_liquidity=qty_liquidity,
            qty_notional=qty_notional,
        )
    return RiskDecision(
        ok=True,
        qty=qty,
        risk_pct=risk_pct,
        binding=binding,
        reason="ok",
        qty_risk=qty_risk,
        qty_liquidity=qty_liquidity,
        qty_notional=qty_notional,
    )
