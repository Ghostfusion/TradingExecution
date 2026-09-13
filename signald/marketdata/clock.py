"""Execution clock: host-vs-reference offset check and session phase (plan §5.3, §9.4).

Invariant: an offset beyond ``config.clock_max_offset_ms`` (50 ms, FINRA 4590)
fails closed - ``ok=False`` and reason ``"clock_offset"`` - so no new order is
sent on a clock that cannot be trusted. Naive and aware stamps are normalised
to UTC-aware before subtraction (naive is read as UTC) so a mixed pair never
raises. Session phase is *not* recomputed here: ``market_phase`` delegates to
``calendar.session_phase`` (one computation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import Config
from .calendar import session_for, session_phase, to_et

#: Offset budget for the reason code (the value itself lives in config).
CLOCK_OFFSET_REASON = "clock_offset"


@dataclass(frozen=True)
class ClockCheck:
    """Result of comparing the host clock against a reference (plan §5.3)."""

    ok: bool
    offset_ms: float
    limit_ms: float
    reason: str


def _as_utc(stamp: datetime) -> datetime:
    """UTC-aware view of a stamp; a naive stamp is read as UTC."""
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def clock_offset_ms(host: datetime, reference: datetime) -> float:
    """Absolute host-vs-reference offset in milliseconds (``timedelta`` division: exact)."""
    delta = _as_utc(host) - _as_utc(reference)
    return abs(delta) / timedelta(milliseconds=1)


def check_clock(host: datetime, reference: datetime, *, config: Config) -> ClockCheck:
    """Pass only when the offset is at or below ``config.clock_max_offset_ms``."""
    offset_ms = clock_offset_ms(host, reference)
    limit_ms = float(config.clock_max_offset_ms)
    ok = offset_ms <= limit_ms
    return ClockCheck(
        ok=ok,
        offset_ms=offset_ms,
        limit_ms=limit_ms,
        reason="" if ok else CLOCK_OFFSET_REASON,
    )


def market_phase(stamp: datetime) -> str:
    """Session phase; delegates to :func:`calendar.session_phase`."""
    return session_phase(stamp)


def next_close(stamp: datetime) -> datetime:
    """First session-close instant strictly after ``stamp``, in ET (plan §5.3)."""
    et = to_et(stamp)
    day = et.date()
    session = session_for(day)
    if session.is_session:
        close = datetime.combine(day, session.close_et, tzinfo=et.tzinfo)
        if et < close:
            return close
    for _ in range(400):
        day += timedelta(days=1)
        session = session_for(day)
        if session.is_session:
            return datetime.combine(day, session.close_et, tzinfo=et.tzinfo)
    # Only reachable outside CALENDAR_YEARS, where every day is fail-closed.
    raise ValueError(f"no session close found after {stamp!r}")


def seconds_since_open(stamp: datetime) -> float | None:
    """Seconds since the ET open while in RTH; ``None`` before, after or closed."""
    et = to_et(stamp)
    if session_phase(et) != "rth":
        return None
    session = session_for(et.date())
    opened = datetime.combine(et.date(), session.open_et, tzinfo=et.tzinfo)
    return (et - opened).total_seconds()
