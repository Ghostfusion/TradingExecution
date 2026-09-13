"""Host-clock offset + session clock tests (plan §5.3, §9.4)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from signald.marketdata import calendar as cal
from signald.marketdata.clock import (
    ClockCheck,
    check_clock,
    clock_offset_ms,
    market_phase,
    next_close,
    seconds_since_open,
)

pytestmark = pytest.mark.timeout(120)

SESSION_DAY = date(2026, 6, 1)  # Monday, full session


# --- offset (FINRA 4590, 50 ms bound) --------------------------------------
def test_offset_under_the_limit_passes(cfg):
    reference = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    host = reference + timedelta(milliseconds=10)
    result = check_clock(host, reference, config=cfg)
    assert isinstance(result, ClockCheck)
    assert result.ok is True
    assert result.offset_ms == 10.0
    assert result.limit_ms == 50.0
    assert result.reason == ""


def test_offset_over_the_limit_fails_closed(cfg):
    reference = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    host = reference + timedelta(milliseconds=51)
    result = check_clock(host, reference, config=cfg)
    assert result.ok is False
    assert result.offset_ms == 51.0
    assert result.reason == "clock_offset"


def test_exact_boundary_is_allowed(cfg):
    reference = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    assert check_clock(reference + timedelta(milliseconds=50), reference, config=cfg).ok is True
    assert check_clock(reference + timedelta(milliseconds=51), reference, config=cfg).ok is False


def test_offset_is_absolute_and_symmetric():
    early = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    late = early + timedelta(milliseconds=250)
    assert clock_offset_ms(late, early) == 250.0
    assert clock_offset_ms(early, late) == 250.0
    assert clock_offset_ms(early, early) == 0.0


def test_naive_and_aware_inputs_do_not_raise(cfg):
    naive = datetime(2026, 6, 1, 13, 30)
    aware = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    assert clock_offset_ms(naive, aware) == 0.0
    assert check_clock(naive, aware, config=cfg).ok is True
    # Naive UTC vs aware ET (EDT, -4): the same instant. A negative timedelta from
    # timezone-aware subtraction must not leak through the absolute offset.
    et = datetime(2026, 6, 1, 9, 30, tzinfo=cal.ET)
    assert clock_offset_ms(naive, et) == 0.0


# --- market_phase delegates to the calendar (one computation) -------------
@pytest.mark.parametrize("hour", [9, 12, 17])
def test_market_phase_matches_the_calendar(hour):
    stamp = datetime.combine(SESSION_DAY, datetime.min.time(), tzinfo=cal.ET).replace(hour=hour)
    assert market_phase(stamp) == cal.session_phase(stamp)


# --- next_close ------------------------------------------------------------
def test_next_close_before_the_open_is_todays_close():
    stamp = datetime.combine(SESSION_DAY, datetime.min.time(), tzinfo=cal.ET).replace(hour=8)
    close = next_close(stamp)
    assert close.astimezone(cal.ET).hour == 16
    assert close.astimezone(cal.ET).date() == SESSION_DAY


def test_next_close_inside_the_session_is_todays_close():
    stamp = datetime.combine(SESSION_DAY, datetime.min.time(), tzinfo=cal.ET).replace(hour=10)
    assert next_close(stamp) == datetime.combine(
        SESSION_DAY, datetime.min.time(), tzinfo=cal.ET
    ).replace(hour=16)


def test_next_close_after_the_close_rolls_to_the_next_session():
    stamp = datetime.combine(SESSION_DAY, datetime.min.time(), tzinfo=cal.ET).replace(hour=17)
    close = next_close(stamp)
    assert close.astimezone(cal.ET).date() == date(2026, 6, 2)
    assert close.astimezone(cal.ET).hour == 16


def test_next_close_from_a_friday_evening_lands_on_monday():
    friday = date(2026, 6, 5)
    stamp = datetime.combine(friday, datetime.min.time(), tzinfo=cal.ET).replace(hour=17)
    assert next_close(stamp).astimezone(cal.ET).date() == date(2026, 6, 8)


def test_next_close_on_a_holiday_lands_on_the_next_session():
    memorial = date(2026, 5, 25)
    stamp = datetime.combine(memorial, datetime.min.time(), tzinfo=cal.ET).replace(hour=12)
    assert next_close(stamp).astimezone(cal.ET).date() == date(2026, 5, 26)


def test_next_close_honours_a_half_day_1300_close():
    half_day = date(2026, 11, 27)
    stamp = datetime.combine(half_day, datetime.min.time(), tzinfo=cal.ET).replace(hour=12)
    close = next_close(stamp)
    assert close.astimezone(cal.ET).date() == half_day
    assert close.astimezone(cal.ET).hour == 13
    after = datetime.combine(half_day, datetime.min.time(), tzinfo=cal.ET).replace(hour=14)
    assert next_close(after).astimezone(cal.ET).date() == date(2026, 11, 30)


# --- seconds_since_open ----------------------------------------------------
def test_seconds_since_open_before_the_open_is_none():
    stamp = datetime.combine(SESSION_DAY, datetime.min.time(), tzinfo=cal.ET).replace(
        hour=9, minute=29
    )
    assert seconds_since_open(stamp) is None


def test_seconds_since_open_at_the_open_is_zero():
    stamp = datetime.combine(SESSION_DAY, datetime.min.time(), tzinfo=cal.ET).replace(
        hour=9, minute=30
    )
    assert seconds_since_open(stamp) == 0.0


def test_seconds_since_open_inside_the_session():
    stamp = datetime.combine(SESSION_DAY, datetime.min.time(), tzinfo=cal.ET).replace(
        hour=10, minute=0
    )
    assert seconds_since_open(stamp) == 1800.0


def test_seconds_since_open_after_the_close_is_none():
    stamp = datetime.combine(SESSION_DAY, datetime.min.time(), tzinfo=cal.ET).replace(hour=16)
    assert seconds_since_open(stamp) is None


def test_seconds_since_open_on_a_weekend_is_none():
    stamp = datetime.combine(date(2026, 6, 6), datetime.min.time(), tzinfo=cal.ET).replace(hour=12)
    assert seconds_since_open(stamp) is None
