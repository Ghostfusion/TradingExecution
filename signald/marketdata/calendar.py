"""Execution-owned session calendar and ET time conversion (plan §1.1, §4.1 C5; design §9).

Invariant: every session fact is **code-shipped and deterministic**. A date
this calendar does not know is *not* a session (``closed_reason =
"calendar_out_of_range"``) - fail closed, never an optimistic guess. There is
no network lookup and no third-party dependency; the US DST rule and the
anonymous Gregorian computus are spelled out here.

Timezone: ``ET`` is the real ``zoneinfo`` ``America/New_York`` when the host
has a tz database. Windows ships none, so when ``zoneinfo`` raises we fall back
to ``US_EASTERN``: a fixed EST/EDT table computed from the post-2007 US rule
(DST starts the 2nd Sunday of March 02:00 local, ends the 1st Sunday of
November 02:00 local) and this module says so rather than silently shifting
every timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --- session constants (regular US equity hours) --------------------------
OPEN_ET = time(9, 30)
CLOSE_ET = time(16, 0)
HALF_DAY_CLOSE_ET = time(13, 0)

#: The calendar years this build ships (plan §9: code-shipped, not a lookup).
CALENDAR_YEARS = (2026, 2027)


# --- zoneinfo, with a documented Windows fallback -------------------------
def _dst_bounds(year: int) -> tuple[datetime, datetime]:
    """Naive-local DST window for ``year`` (2nd Sun Mar 02:00 -> 1st Sun Nov 02:00)."""
    start = datetime.combine(_nth_weekday(year, 3, 6, 2), time(2, 0))
    end = datetime.combine(_nth_weekday(year, 11, 6, 1), time(2, 0))
    return start, end


class _USEastern(tzinfo):
    """Fixed EST/EDT from the post-2007 US DST rule (no tz database needed)."""

    def _is_dst_local(self, nd: datetime) -> bool:
        start, end = _dst_bounds(nd.year)
        return start <= nd < end

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        if dt is None:
            return None
        hours = -4 if self._is_dst_local(dt.replace(tzinfo=None)) else -5
        return timedelta(hours=hours)

    def dst(self, dt: datetime | None) -> timedelta | None:
        if dt is None:
            return None
        return timedelta(hours=1) if self._is_dst_local(dt.replace(tzinfo=None)) else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        if dt is None:
            return "EST"
        return "EDT" if self._is_dst_local(dt.replace(tzinfo=None)) else "EST"

    def fromutc(self, dt: datetime) -> datetime:
        nd = dt.replace(tzinfo=None)
        local = nd + timedelta(hours=-5)
        if self._is_dst_local(local):
            local = nd + timedelta(hours=-4)
        return local.replace(tzinfo=self)


#: The fallback instance, exported so the DST rule can be tested directly.
US_EASTERN = _USEastern()

try:
    ET: tzinfo = ZoneInfo("America/New_York")
except (ZoneInfoNotFoundError, OSError):
    # No host tz database (typical on Windows without the `tzdata` wheel):
    # the derived EST/EDT table above is used, and said so in the docstring.
    ET = US_EASTERN


# --- calendar primitives --------------------------------------------------
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """``n``-th ``weekday`` (Mon=0) of ``month``; e.g. 3rd Monday of January."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Last ``weekday`` (Mon=0) of ``month``; e.g. last Monday of May."""
    next_first = date(year + (month == 12), month % 12 + 1, 1)
    last = next_first - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def observed(day: date) -> date:
    """Federal observed-day rule: Saturday -> the preceding Friday, Sunday -> the Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian computus (Meeus/Jones/Butcher) - ``dateutil`` is not a dependency."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _holiday_dates(year: int) -> set[date]:
    """The ten US market holidays for ``year``, observed-day rule applied."""
    return {
        observed(date(year, 1, 1)),            # New Year's Day
        _nth_weekday(year, 1, 0, 3),           # MLK Day
        _nth_weekday(year, 2, 0, 3),           # Presidents' Day
        easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),             # Memorial Day
        observed(date(year, 6, 19)),           # Juneteenth
        observed(date(year, 7, 4)),            # Independence Day
        _nth_weekday(year, 9, 0, 1),           # Labor Day
        _nth_weekday(year, 11, 3, 4),          # Thanksgiving
        observed(date(year, 12, 25)),          # Christmas
    }


#: Explicit holiday dates, computed from the rules above (plan §4.1 C5).
#: 2026: 01-01, 01-19, 02-16, 04-03, 05-25, 06-19, 07-03, 09-07, 11-26, 12-25
#: 2027: 01-01, 01-18, 02-15, 03-26, 05-31, 06-18, 07-05, 09-06, 11-25, 12-24
US_HOLIDAYS_2026_2027: frozenset[date] = frozenset(
    _holiday_dates(2026) | _holiday_dates(2027)
)


def _half_day_close(day: date) -> time | None:
    """13:00 close for the day after Thanksgiving, Jul 3 and Dec 24 (sessions only)."""
    if day.weekday() >= 5 or day in US_HOLIDAYS_2026_2027:
        return None
    if day == _nth_weekday(day.year, 11, 3, 4) + timedelta(days=1):
        return HALF_DAY_CLOSE_ET
    if (day.month, day.day) in {(7, 3), (12, 24)}:
        return HALF_DAY_CLOSE_ET
    return None


@dataclass(frozen=True)
class Session:
    """One calendar day's session facts (plan §4.1 C5)."""

    date: date
    is_session: bool
    open_et: time | None
    close_et: time | None
    is_half_day: bool
    closed_reason: str | None


def session_for(day: date) -> Session:
    """Session facts for ``day``; an unknown year is closed, never assumed open."""
    if day.year not in CALENDAR_YEARS:
        return Session(day, False, None, None, False, "calendar_out_of_range")
    if day.weekday() >= 5:
        return Session(day, False, None, None, False, "weekend")
    if day in US_HOLIDAYS_2026_2027:
        return Session(day, False, None, None, False, "holiday")
    half_close = _half_day_close(day)
    return Session(
        date=day,
        is_session=True,
        open_et=OPEN_ET,
        close_et=half_close or CLOSE_ET,
        is_half_day=half_close is not None,
        closed_reason=None,
    )


def is_session(day: date) -> bool:
    """True only when the whole regular trading day exists."""
    return session_for(day).is_session


def trading_days(start: date, end: date) -> tuple[date, ...]:
    """Ascending sessions in ``[start, end]``; an inverted span is empty."""
    if end < start:
        return ()
    days: list[date] = []
    day = start
    while day <= end:
        if is_session(day):
            days.append(day)
        day += timedelta(days=1)
    return tuple(days)


# --- time conversion ------------------------------------------------------
def to_et(stamp: datetime) -> datetime:
    """Convert to aware ET; a naive stamp is read as UTC (the envelope convention)."""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(ET)


def to_utc(stamp_et: datetime) -> datetime:
    """Convert to aware UTC; a naive stamp is read as ET."""
    if stamp_et.tzinfo is None:
        stamp_et = stamp_et.replace(tzinfo=ET)
    return stamp_et.astimezone(UTC)


def session_phase(stamp: datetime) -> str:
    """``"pre" | "rth" | "post" | "closed"`` from the ET clock (plan §4.1 C5)."""
    et = to_et(stamp)
    session = session_for(et.date())
    if not session.is_session:
        return "closed"
    clock = et.time()
    if clock < session.open_et:
        return "pre"
    if clock < session.close_et:
        return "rth"
    return "post"
