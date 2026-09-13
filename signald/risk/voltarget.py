"""EWMA volatility and the vol-target scalar (plan §6 formulas 1-2, design §10).

One computation, one module: :func:`ewma_vol` is the only producer of an
annualised EWMA sigma and :func:`vol_scalar` is the only decider of the target
scalar. The sizer consumes the returned ``scalar`` verbatim; no sleeve, gate or
report recomputes either formula (plan §6 closing rule).

Invariants:

- **Fail closed.** A non-positive half-life or periods-per-year, a non-finite
  return, and an out-of-domain scalar parameter raise ``ValueError`` instead of
  yielding a number; the caller turns that into a BLOCK.
- **Honest degradation.** Fewer than ``warmup_days`` observations return exactly
  ``scalar = 1.0`` with ``warmup_ok=False`` and ``applied=False``: scaling up
  before the warm-up (design §10: >=270 trading days) is forbidden, and the
  caller can see that no scaling happened.
- **The band is visible.** ``applied`` is True only when the clamp actually
  moved the scalar beyond the rebalance band; when the band suppresses a change
  the returned ``scalar`` is ``current`` verbatim and ``reason`` says so, so a
  caller cannot mistake it for a freshly computed value.
- The *computed* scalar is always clamped to ``[floor, cap]``; the warm-up and
  band-suppressed paths return the unscaled/current value by design.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["VolScalar", "ewma_vol", "vol_scalar"]

#: Closed set of reasons a scalar was not applied as computed.
REASONS = ("warmup", "within_band", "applied", "zero_vol")


def ewma_vol(
    returns: Sequence[float],
    *,
    halflife_days: float = 20.0,
    periods_per_year: int = 252,
) -> float:
    """Annualised zero-mean EWMA sigma; ``returns[0]`` is the most recent.

    ``lambda = ln(2) / halflife_days`` and
    ``var = (1 - lambda) * sum(lambda**i * returns[i]**2)``; the result is
    ``sqrt(var * periods_per_year)``. An empty series is ``0.0`` (no variance is
    invented from no data).
    """
    if not halflife_days > 0:
        raise ValueError(f"halflife_days must be positive, got {halflife_days!r}")
    if not periods_per_year > 0:
        raise ValueError(f"periods_per_year must be positive, got {periods_per_year!r}")
    if not returns:
        return 0.0

    decay = math.log(2.0) / halflife_days
    variance = 0.0
    weight = 1.0
    for r in returns:
        if not math.isfinite(r):
            raise ValueError(f"returns must be finite, got {r!r}")
        variance += weight * r * r
        weight *= decay
    variance *= 1.0 - decay
    return math.sqrt(variance * periods_per_year)


@dataclass(frozen=True)
class VolScalar:
    """The vol-target decision: the value to size with plus why it is that value."""

    scalar: float
    """What the sizer must use (the applied value)."""

    target: float
    """The sleeve's annualised vol target."""

    realized: float
    """Annualised realized vol from the same estimator."""

    warmup_ok: bool
    """False when there was too little history to scale at all."""

    applied: bool
    """False when the band suppressed the change (or no change was allowed)."""

    reason: str
    """One of :data:`REASONS`: ``warmup`` | ``within_band`` | ``applied`` | ``zero_vol``."""


def vol_scalar(
    returns: Sequence[float],
    *,
    target: float,
    halflife_days: float = 20.0,
    warmup_days: int = 270,
    cap: float = 1.5,
    floor: float = 0.10,
    band: float = 0.12,
    current: float = 1.0,
) -> VolScalar:
    """Clamped ``target / realized`` scalar with warm-up and a rebalance band.

    Fewer than ``warmup_days`` observations scale nothing (``scalar = 1.0``).
    Zero realized vol with enough history returns ``cap`` without dividing by
    zero. Otherwise ``raw = clamp(target / realized, floor, cap)`` is applied
    only when it moves more than ``band`` relative to ``current``.
    """
    if not target > 0:
        raise ValueError(f"target must be positive, got {target!r}")
    if not cap > 0:
        raise ValueError(f"cap must be positive, got {cap!r}")
    if not floor > 0:
        raise ValueError(f"floor must be positive, got {floor!r}")
    if floor > cap:
        raise ValueError(f"floor {floor!r} exceeds cap {cap!r}")
    if not band >= 0:
        raise ValueError(f"band must be non-negative, got {band!r}")
    if not current > 0:
        raise ValueError(f"current must be positive, got {current!r}")
    if warmup_days < 0:
        raise ValueError(f"warmup_days must be non-negative, got {warmup_days!r}")

    realized = ewma_vol(returns, halflife_days=halflife_days)

    if len(returns) < warmup_days:
        return VolScalar(
            scalar=1.0,
            target=target,
            realized=realized,
            warmup_ok=False,
            applied=False,
            reason="warmup",
        )

    if realized == 0.0:
        return VolScalar(
            scalar=cap,
            target=target,
            realized=realized,
            warmup_ok=True,
            applied=False,
            reason="zero_vol",
        )

    raw = min(cap, max(floor, target / realized))
    if abs(raw - current) / current > band:
        return VolScalar(
            scalar=raw,
            target=target,
            realized=realized,
            warmup_ok=True,
            applied=True,
            reason="applied",
        )
    return VolScalar(
        scalar=current,
        target=target,
        realized=realized,
        warmup_ok=True,
        applied=False,
        reason="within_band",
    )
