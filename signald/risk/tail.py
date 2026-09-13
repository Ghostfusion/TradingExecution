"""ES/CVaR estimation: three estimators, the worst wins (plan §6.5, design §10).

One number per decision: the 1-day 97.5% expected shortfall of the book (or a
cluster) as a *fraction of equity*. Three estimators are always computed and the
largest is returned, so an under-estimated historical tail can never weaken the
budget check. Historical ES alone is not acceptable (design §10 "ES estimation":
parametric + EWMA-ES + historical-ES + stress grid, "never historical-ES alone")
because ~6 tail observations a year is statistically meaningless on its own.

Invariants:

* A *loss* series is a series of finite non-negative fractions - a loss series,
  not a return series. ``es_historical`` rejects anything else rather than
  guessing a sign, and the gate turns the raise into a BLOCK (fail closed).
* A ``0.0`` sigma with no history never yields ``0.0`` ES: the stress grid binds
  and the estimate is flagged (design C10: "insufficient history -> conservative
  ES (stress grid), flagged").
* Every public value is non-negative, and every input the formulas cannot
  evaluate raises ``ValueError`` instead of producing a placeholder number.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "ESResult",
    "StressGrid",
    "es_estimate",
    "es_historical",
    "es_parametric",
    "es_stress",
    "z_score",
]


@dataclass(frozen=True)
class StressGrid:
    """The stress scenarios the ES engine always evaluates (plan §6.5, design §10).

    ``gap_pct`` and ``halt_pct`` are shocks on a position/cluster of the given
    ``weight`` (its share of equity), so a 5% position facing a 10% gap loses
    0.5% of equity. ``vol_mult`` is the multiplicative volatility shock.
    """

    vol_mult: float = 2.0
    gap_pct: float = 0.10
    halt_pct: float = 0.20
    horizon_days: int = 1


@dataclass(frozen=True)
class ESResult:
    """One ES estimate: the worst of the three estimators plus its provenance.

    ``value_pct`` is a fraction of equity (``0.011`` == 1.1%). ``components``
    always carries all three estimator values under ``historical``,
    ``parametric`` and ``stress`` so a caller can see why the binding one won.
    """

    value_pct: float
    estimator: str
    flagged: bool
    window_days: int
    components: dict[str, float]


def _check_confidence(confidence: float) -> float:
    """Reject anything that is not a probability in (0, 1) - fail closed."""
    if not math.isfinite(confidence) or not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be a probability in (0, 1): {confidence!r}")
    return float(confidence)


def _check_sigma(sigma_daily: float) -> float:
    """Reject a sigma that cannot be a daily volatility - fail closed."""
    if not math.isfinite(sigma_daily) or sigma_daily < 0.0:
        raise ValueError(f"sigma_daily must be finite and non-negative: {sigma_daily!r}")
    return float(sigma_daily)


def _clamp_unit(weight: float) -> float:
    """Clamp an equity share to [0, 1]; a non-finite weight fails closed to 1.0."""
    if not math.isfinite(weight):
        return 1.0
    return min(1.0, max(0.0, float(weight)))


def z_score(confidence: float) -> float:
    """Standard-normal quantile for ``confidence`` (0.975 -> ~1.9600)."""
    return float(statistics.NormalDist().inv_cdf(_check_confidence(confidence)))


def es_historical(losses: Sequence[float], confidence: float) -> float:
    """Mean of the worst ``ceil((1 - confidence) * n)`` losses.

    ``losses`` is a loss *series*: finite, non-negative fractions. Raises
    ``ValueError`` on an empty series or on a negative/non-finite element.
    """
    _check_confidence(confidence)
    if not losses:
        raise ValueError("es_historical requires at least one loss observation")
    for loss in losses:
        if not math.isfinite(loss) or loss < 0.0:
            raise ValueError(
                f"losses must be finite non-negative fractions (a loss series, "
                f"not a return series): {loss!r}"
            )
    ordered = sorted(losses, reverse=True)
    tail = max(1, math.ceil((1.0 - confidence) * len(ordered)))
    return float(statistics.fmean(ordered[:tail]))


def es_parametric(sigma_daily: float, confidence: float, horizon: int = 1) -> float:
    """Parametric (normal) ES: ``z * sigma * sqrt(horizon)``."""
    if horizon < 1:
        raise ValueError(f"horizon must be at least one day: {horizon!r}")
    sigma = _check_sigma(sigma_daily)
    return z_score(confidence) * sigma * math.sqrt(horizon)


def es_stress(
    sigma_daily: float,
    confidence: float,
    *,
    grid: StressGrid = StressGrid(),  # noqa: B008 - frozen dataclass default, safe to share
    weight: float = 1.0,
) -> float:
    """Worst of the vol, gap and halt shocks on a position/cluster of ``weight``.

    The vol shock is the parametric ES with sigma multiplied by ``grid.vol_mult``
    (and the grid's horizon); the gap and halt shocks are their percentage times
    ``weight`` (clamped to [0, 1]).
    """
    share = _clamp_unit(weight)
    vol_shock = es_parametric(sigma_daily * grid.vol_mult, confidence, grid.horizon_days)
    gap_shock = grid.gap_pct * share
    halt_shock = grid.halt_pct * share
    return max(vol_shock, gap_shock, halt_shock)


def es_estimate(
    losses: Sequence[float],
    *,
    confidence: float,
    sigma_daily: float | None = None,
    grid: StressGrid = StressGrid(),  # noqa: B008 - frozen dataclass default, safe to share
    weight: float = 1.0,
    min_observations: int = 30,
) -> ESResult:
    """Worst of the three estimators, named and flagged (plan §6.5).

    ``sigma_daily`` defaults to the sample stdev of ``losses``. ``value_pct`` is
    ``max(historical, parametric, stress)`` and ``estimator`` names the binding
    one (``stress`` wins ties - the conservative label). ``flagged`` is True when
    the losses are fewer than ``min_observations`` or the effective sigma is
    non-positive, i.e. history was insufficient to trust the estimate.
    """
    _check_confidence(confidence)
    window_days = len(losses)
    if sigma_daily is None:
        sigma = float(statistics.stdev(losses)) if window_days >= 2 else 0.0
    else:
        sigma = _check_sigma(sigma_daily)

    # An empty series contributes no historical estimate; the stress grid and the
    # flag carry the decision instead of a fabricated number.
    historical = es_historical(losses, confidence) if losses else 0.0
    parametric = es_parametric(sigma, confidence, grid.horizon_days)
    stress = es_stress(sigma, confidence, grid=grid, weight=weight)
    components = {"historical": historical, "parametric": parametric, "stress": stress}

    value_pct = max(components.values())
    if stress >= value_pct:
        estimator = "stress"
    elif parametric >= value_pct:
        estimator = "parametric"
    else:
        estimator = "historical"
    flagged = window_days < min_observations or sigma <= 0.0
    return ESResult(
        value_pct=value_pct,
        estimator=estimator,
        flagged=flagged,
        window_days=window_days,
        components=components,
    )
