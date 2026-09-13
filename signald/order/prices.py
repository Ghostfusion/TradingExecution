"""The price tick rule (design §8.2) - one owner, imported by policy and guard.

Alpaca rejects sub-penny prices: at or above $1 the tick is $0.01, below $1 it
is $0.0001. Rounding and *validating* are different operations, so both live
here and both read the same constants - a caller that produces a price and a
caller that checks one can never disagree about the grid.
"""

from __future__ import annotations

import math
from fractions import Fraction

TICK_LARGE = 0.01  # prices >= $1.00
TICK_SMALL = 0.0001  # prices < $1.00
LARGE_PRICE_THRESHOLD = 1.0


def tick_for(price: float) -> float:
    """The tick size a price of this magnitude must sit on."""
    return TICK_LARGE if abs(price) >= LARGE_PRICE_THRESHOLD else TICK_SMALL


def round_price(price: float, *, tick: float | None = None) -> float:
    """Round a price onto the tick grid (documented default: by magnitude).

    Raises ``ValueError`` for a non-positive tick so a caller cannot round to
    nowhere. Deterministic: it rounds to the nearest grid point, ties upward.
    """
    value = float(price)
    if not math.isfinite(value):  # NaN / +-inf
        raise ValueError(f"price is not finite: {price!r}")
    step = tick_for(value) if tick is None else float(tick)
    if step <= 0:
        raise ValueError(f"tick must be positive: {tick!r}")
    if step == TICK_LARGE:
        return round(value, 2)
    if step == TICK_SMALL:
        return round(value, 4)
    # an explicit non-standard tick: snap with exact rational arithmetic
    return float(Fraction(round(value / step)) * Fraction(step))


def price_is_on_grid(price: float, *, tick: float | None = None) -> bool:
    """True when ``price`` already sits on the tick grid (the validator half)."""
    try:
        step = tick_for(price) if tick is None else float(tick)
        if step <= 0:
            return False
        return round_price(price, tick=step) == float(price)
    except (TypeError, ValueError):
        return False
