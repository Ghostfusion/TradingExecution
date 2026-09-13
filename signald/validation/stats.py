"""Validation statistics: pure functions of a series that decide if a backtest means anything.

Invariant (plan §6 formula 11, §11.1-11.2; design §11). A backtest whose trial
count N is not recorded is unfalsifiable [V1][V3]; these functions therefore take
N as a first-class input, and a metric that cannot be computed is reported as
unavailable (:class:`ValidationGate` with ``passed=None`` and a reason), never as
a fabricated number. Every function here is a pure function of its inputs; the
only randomness is the seeded, deterministic stationary bootstrap (Politis-Romano)
used for the paired comparison interval.

Numbers implemented once, here, and never recomputed by a sleeve, gate or report:

* Deflated Sharpe Ratio - Bailey & Lopez de Prado, DSR at the expected maximum
  Sharpe under the null (plan §11.1, design §11.1).
* Probability of backtest overfitting - CSCV (plan §11.2, design §11.2).
* Minimum backtest length - plan §6 formula 11, ``(E[max_N]/SR*)^2`` years.
* Paired stationary-bootstrap comparison of two sleeves' daily PnL difference
  (plan §10.3, design §11.4): a *paired* interval, never two point estimates.
* Newey-West HAC t-statistic of the mean (design §11.2 "HAC/Newey-West t-stats").
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = [
    "ValidationGate",
    "deflated_sharpe",
    "minimum_backtest_length",
    "newey_west_t",
    "paired_comparison",
    "probability_of_backtest_overfitting",
    "stationary_bootstrap_ci",
    "validation_gates",
]

#: Euler-Mascheroni constant, the weight in the expected-maximum-Sharpe formula.
EULER_MASCHERONI = 0.5772156649015329

#: Plan §11.2 gate thresholds (pre-registered; design §11.6).
DSR_THRESHOLD = 0.95
PBO_THRESHOLD = 0.05
MIN_TRADES = 50
MIN_OOS_MONTHS = 6.0


@dataclass(frozen=True)
class ValidationGate:
    """One pre-registered gate: what was measured, the bar, and the verdict.

    ``passed is None`` means the gate is *not computable* from the inputs given
    (fail closed): the reason says why, and no number is invented.
    """

    name: str
    value: float | None
    threshold: float | None
    passed: bool | None
    reason: str


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _normal() -> statistics.NormalDist:
    return statistics.NormalDist()


def _sharpe(values: Sequence[float]) -> float:
    """Mean/sample-stdev. A zero-variance or short sample carries no information -> 0.0."""
    if len(values) < 2:
        return 0.0
    sigma = statistics.stdev(values)
    if sigma == 0.0:
        return 0.0
    return statistics.fmean(values) / sigma


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile of an already-sorted sequence (q in [0, 1])."""
    n = len(sorted_values)
    if n == 1:
        return float(sorted_values[0])
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    return float(sorted_values[lo]) * (hi - pos) + float(sorted_values[hi]) * (pos - lo)


def _percentile_interval(samples: Sequence[float], confidence: float) -> tuple[float, float]:
    """Two-sided percentile interval of a bootstrap distribution."""
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1): {confidence}")
    alpha = (1.0 - confidence) / 2.0
    ordered = sorted(samples)
    return _quantile(ordered, alpha), _quantile(ordered, 1.0 - alpha)


def _expected_max_factor(n_trials: int) -> float:
    """Expected maximum of ``n_trials`` standard-normal Sharpe estimates (Bailey/LdP Eq. 1).

    Returns ``(1-g)*z^-1(1-1/N) + g*z^-1(1-1/(N*e))`` with g the Euler-Mascheroni
    constant. For a single trial the expected maximum of a zero-mean estimate is
    the mean itself, 0.0 - the N=1 case where the approximation is degenerate.
    """
    if n_trials <= 1:
        return 0.0
    nd = _normal()
    high = nd.inv_cdf(1.0 - 1.0 / n_trials)
    low = nd.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return (1.0 - EULER_MASCHERONI) * high + EULER_MASCHERONI * low


def _contiguous_blocks(values: Sequence[float], splits: int) -> list[list[float]]:
    """Split a series into ``splits`` contiguous, near-equal blocks (deterministic)."""
    n = len(values)
    base, extra = divmod(n, splits)
    blocks: list[list[float]] = []
    start = 0
    for i in range(splits):
        size = base + (1 if i < extra else 0)
        blocks.append(list(values[start : start + size]))
        start += size
    return blocks


# --------------------------------------------------------------------------
# deflated Sharpe
# --------------------------------------------------------------------------
def deflated_sharpe(
    *,
    sharpe: float,
    n_trials: int,
    observations: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Bailey & Lopez de Prado Deflated Sharpe Ratio (plan §11.1, design §11.1).

    ``sr0 = sqrt(var_sr) * expected_max_factor(N)`` with the documented fallback
    ``var_sr = 1 / observations`` when the caller has no better estimate of the
    Sharpe variance across trials.  Then
    ``DSR = Phi((sharpe - sr0) * sqrt(observations - 1) / sqrt(1 - skew*sharpe +
    (kurtosis - 1)/4 * sharpe**2))``. A zero Sharpe with a single trial is exactly
    0.5: no evidence either way.

    ``n_trials < 1`` or ``observations < 2`` raises :class:`ValueError` (an
    unusable input); a non-positive non-normality correction raises too.
    """
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1: {n_trials}")
    if observations < 2:
        raise ValueError(f"observations must be >= 2: {observations}")
    var_sr = 1.0 / observations
    sr0 = math.sqrt(var_sr) * _expected_max_factor(n_trials)
    correction = 1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe**2
    if correction <= 0.0:
        raise ValueError(f"non-normality correction is not positive: {correction}")
    z = (sharpe - sr0) * math.sqrt(observations - 1) / math.sqrt(correction)
    return _normal().cdf(z)


# --------------------------------------------------------------------------
# probability of backtest overfitting (CSCV)
# --------------------------------------------------------------------------
def probability_of_backtest_overfitting(
    returns: Sequence[float],
    *,
    splits: int = 8,
    trials: int = 1,
) -> float | None:
    """CSCV estimate of the probability of backtest overfitting (plan §11.2).

    Full CSCV ranks the in-sample-best of N trial series on the same out-of-sample
    blocks. A single series has no such candidate matrix, so the split is taken
    walk-forward: the series is cut into ``splits`` contiguous blocks and evaluated
    at every in/out boundary ``k`` (in-sample = blocks before ``k``, out-of-sample =
    blocks after). A configuration is *chosen* when its in-sample Sharpe is among
    the top ``1/trials`` of the boundaries (``trials`` is how many candidates the
    search tried and therefore how selective the choice is; the default 1 lets
    every boundary speak). PBO is the share of those chosen boundaries whose
    out-of-sample Sharpe is below their in-sample Sharpe - a decaying edge (a
    "strictly better first half") scores 1.0, a stationary series ~0.5.

    Deterministic (no RNG). Returns ``None`` when there are fewer than
    ``splits * 4`` observations (an honest "cannot estimate", not 0). Raises
    :class:`ValueError` only for an empty series, ``splits < 2`` or ``trials < 1``.
    """
    n = len(returns)
    if n == 0:
        raise ValueError("returns is empty")
    if splits < 2:
        raise ValueError(f"splits must be >= 2: {splits}")
    if trials < 1:
        raise ValueError(f"trials must be >= 1: {trials}")
    if n < splits * 4:
        return None

    blocks = _contiguous_blocks(returns, splits)
    in_sharpe: list[float] = []
    out_sharpe: list[float] = []
    for k in range(1, splits):
        in_sample = [v for block in blocks[:k] for v in block]
        out_sample = [v for block in blocks[k:] for v in block]
        in_sharpe.append(_sharpe(in_sample))
        out_sharpe.append(_sharpe(out_sample))

    take = max(1, round(len(in_sharpe) / trials))
    chosen = sorted(range(len(in_sharpe)), key=lambda i: (-in_sharpe[i], i))[:take]
    losers = sum(1 for i in chosen if out_sharpe[i] < in_sharpe[i])
    return losers / len(chosen)


# --------------------------------------------------------------------------
# minimum backtest length
# --------------------------------------------------------------------------
def minimum_backtest_length(*, n_trials: int, sharpe: float) -> float:
    """Minimum backtest length in YEARS for a per-year (annualised) Sharpe.

    Plan §6 formula 11: ``MinBTL = (E[max_N] / SR*)^2`` where ``E[max_N]`` is the
    expected maximum Sharpe under the null for ``n_trials`` configurations (the
    DSR engine; ``2*ln(N)/SR^2`` is its upper-bound simplification). A
    non-positive Sharpe cannot establish anything, so the requirement is infinite.
    """
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1: {n_trials}")
    if sharpe <= 0.0:
        return math.inf
    return (_expected_max_factor(n_trials) / sharpe) ** 2


# --------------------------------------------------------------------------
# stationary bootstrap
# --------------------------------------------------------------------------
def _stationary_sample(
    values: Sequence[float], rng: random.Random, mean_block: float, n: int
) -> list[float]:
    """One Politis-Romano stationary-bootstrap resample (geometric block lengths)."""
    p = 1.0 / mean_block
    out: list[float] = []
    idx = rng.randrange(n)
    for _ in range(n):
        out.append(values[idx])
        idx = rng.randrange(n) if rng.random() < p else (idx + 1) % n
    return out


def stationary_bootstrap_ci(
    values: Sequence[float],
    *,
    statistic: Callable[[Sequence[float]], float],
    resamples: int = 2000,
    mean_block: float | None = None,
    confidence: float = 0.95,
    seed: int = 7,
) -> tuple[float, float]:
    """Seeded stationary-bootstrap percentile interval of ``statistic`` (plan §10.3).

    The mean block length defaults to ``n**(1/3)``. Deterministic for a fixed
    ``seed``. The interval - never a point estimate alone - is the requirement
    for a paired comparison (design §11.4).
    """
    n = len(values)
    if n == 0:
        raise ValueError("values is empty")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1): {confidence}")
    if resamples < 1:
        raise ValueError(f"resamples must be >= 1: {resamples}")
    block = float(mean_block) if mean_block is not None else n ** (1.0 / 3.0)
    if block <= 0.0:
        raise ValueError(f"mean_block must be positive: {mean_block}")
    rng = random.Random(seed)
    samples = [statistic(_stationary_sample(values, rng, block, n)) for _ in range(resamples)]
    return _percentile_interval(samples, confidence)


def paired_comparison(
    a: Sequence[float],
    b: Sequence[float],
    *,
    resamples: int = 2000,
    seed: int = 7,
) -> dict[str, float | int | bool]:
    """Paired stationary-bootstrap interval on the daily-PnL difference ``a - b``.

    Design §11.4: two sleeves on the same universe/period are compared
    trade-for-trade; the honest output is the interval on the difference, not two
    Sharpe point estimates. ``excludes_zero`` is true only when the whole interval
    lies on one side of zero.
    """
    if len(a) != len(b):
        raise ValueError(f"paired series must be the same length: {len(a)} != {len(b)}")
    if not a:
        raise ValueError("paired series is empty")
    diffs = [float(x) - float(y) for x, y in zip(a, b, strict=True)]
    ci_low, ci_high = stationary_bootstrap_ci(
        diffs, statistic=statistics.fmean, resamples=resamples, seed=seed
    )
    mean_diff = statistics.fmean(diffs)
    share_positive = sum(1 for d in diffs if d > 0.0) / len(diffs)
    return {
        "mean_diff": mean_diff,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "share_positive": share_positive,
        "n": len(diffs),
        "excludes_zero": ci_low > 0.0 or ci_high < 0.0,
    }


# --------------------------------------------------------------------------
# Newey-West HAC t-statistic
# --------------------------------------------------------------------------
def newey_west_t(values: Sequence[float], *, lags: int | None = None) -> float:
    """HAC (Newey-West, Bartlett kernel) t-statistic of the mean (design §11.2).

    ``lags`` defaults to the rule of thumb ``floor(4*(n/100)**(2/9))``, clamped to
    ``n - 1``. A zero long-run variance (a constant series, or a single
    observation) yields 0.0 - undefined-safe, never a division error.
    """
    x = [float(v) for v in values]
    n = len(x)
    if n == 0:
        raise ValueError("values is empty")
    if lags is None:
        lags = int(4.0 * (n / 100.0) ** (2.0 / 9.0))
    if lags < 0:
        raise ValueError(f"lags must be >= 0: {lags}")
    lags = min(lags, n - 1)
    mean = statistics.fmean(x)
    resid = [v - mean for v in x]
    long_run = sum(e * e for e in resid) / n
    for k in range(1, lags + 1):
        weight = 1.0 - k / (lags + 1)
        gamma_k = sum(resid[i] * resid[i - k] for i in range(k, n)) / n
        long_run += 2.0 * weight * gamma_k
    variance_of_mean = long_run / n
    if variance_of_mean <= 0.0:
        return 0.0
    return mean / math.sqrt(variance_of_mean)


# --------------------------------------------------------------------------
# the pre-registered gate set
# --------------------------------------------------------------------------
def validation_gates(
    *,
    sharpe: float,
    n_trials: int,
    observations: int,
    trades: int,
    oos_months: float,
    returns: Sequence[float] | None = None,
) -> tuple[ValidationGate, ...]:
    """Plan §11.2 gate set: DSR >= 0.95, PBO <= 0.05, trades >= 50, OOS >= 6 months, N recorded.

    Each gate reports its value, threshold, verdict and reason. A gate whose
    input is missing or unusable reports ``passed=None`` (not computable) and a
    reason naming the gap - it never counts as a pass and never invents a number.
    """
    dsr_value: float | None
    try:
        dsr_value = deflated_sharpe(sharpe=sharpe, n_trials=n_trials, observations=observations)
        dsr_reason = f"DSR {dsr_value:.4f} vs {DSR_THRESHOLD}"
    except ValueError as exc:
        dsr_value = None
        dsr_reason = f"DSR not computable: {exc}"

    pbo_value: float | None = None
    if returns is None:
        pbo_reason = "PBO not computable: no return series"
    else:
        try:
            pbo_value = probability_of_backtest_overfitting(returns)
        except ValueError as exc:
            pbo_value = None
            pbo_reason = f"PBO not computable: {exc}"
        else:
            pbo_reason = (
                "PBO not computable: too few observations for the split"
                if pbo_value is None
                else f"PBO {pbo_value:.4f} vs {PBO_THRESHOLD}"
            )

    return (
        ValidationGate(
            name="dsr",
            value=dsr_value,
            threshold=DSR_THRESHOLD,
            passed=None if dsr_value is None else dsr_value >= DSR_THRESHOLD,
            reason=dsr_reason,
        ),
        ValidationGate(
            name="pbo",
            value=pbo_value,
            threshold=PBO_THRESHOLD,
            passed=None if pbo_value is None else pbo_value <= PBO_THRESHOLD,
            reason=pbo_reason,
        ),
        ValidationGate(
            name="trades",
            value=float(trades),
            threshold=float(MIN_TRADES),
            passed=trades >= MIN_TRADES,
            reason=f"{trades} trades vs {MIN_TRADES}",
        ),
        ValidationGate(
            name="oos_months",
            value=float(oos_months),
            threshold=MIN_OOS_MONTHS,
            passed=oos_months >= MIN_OOS_MONTHS,
            reason=f"{oos_months} OOS months vs {MIN_OOS_MONTHS}",
        ),
        ValidationGate(
            name="n_trials",
            value=float(n_trials),
            threshold=None,
            passed=n_trials >= 1,
            reason=(
                f"N={n_trials} trials recorded"
                if n_trials >= 1
                else "trial count N not recorded"
            ),
        ),
    )
