"""Session calendar + ET conversion tests (plan §4.1 C5, design §9)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from signald.marketdata import calendar as cal_module
from signald.marketdata.calendar import (
    CLOSE_ET,
    ET,
    HALF_DAY_CLOSE_ET,
    OPEN_ET,
    US_EASTERN,
    US_HOLIDAYS_2026_2027,
    Session,
    easter_sunday,
    is_session,
    observed,
    session_for,
    session_phase,
    to_et,
    to_utc,
    trading_days,
)

pytestmark = pytest.mark.timeout(120)

# Hand-computed from the rules in the module (observed day applied).
EXPECTED_HOLIDAYS = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),
    }
)

# A plain Monday session used throughout (Memorial 2026 is 05-25, so 06-01 is open).
SESSION_DAY = date(2026, 6, 1)


# --- holidays --------------------------------------------------------------
def test_holiday_table_matches_the_rules():
    assert US_HOLIDAYS_2026_2027 == EXPECTED_HOLIDAYS


@pytest.mark.parametrize("holiday", sorted(EXPECTED_HOLIDAYS))
def test_every_shipped_holiday_is_a_non_session(holiday):
    session = session_for(holiday)
    assert session.is_session is False
    assert session.open_et is None and session.close_et is None
    assert session.closed_reason == "holiday"
    assert is_session(holiday) is False


def test_good_friday_is_the_holiday_two_days_before_easter():
    assert date(2026, 4, 3) in US_HOLIDAYS_2026_2027
    assert date(2027, 3, 26) in US_HOLIDAYS_2026_2027


# --- observed-day rule (Saturday -> preceding Friday, Sunday -> Monday) -----
@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 1, 1), date(2026, 1, 1)),      # Thursday: unchanged
        (date(2027, 6, 19), date(2027, 6, 18)),    # Saturday -> Friday
        (date(2027, 7, 4), date(2027, 7, 5)),      # Sunday -> Monday
        (date(2028, 1, 1), date(2027, 12, 31)),    # New Year's on a Saturday
        (date(2026, 7, 4), date(2026, 7, 3)),      # Independence Day on a Saturday
    ],
)
def test_observed_day_rule(day, expected):
    assert observed(day) == expected


def test_observed_saturday_holidays_land_on_the_encoded_dates():
    assert observed(date(2027, 6, 19)) == date(2027, 6, 18)
    assert observed(date(2027, 12, 25)) == date(2027, 12, 24)


# --- session classification -----------------------------------------------
def test_a_normal_weekday_is_a_session():
    session = session_for(SESSION_DAY)
    assert isinstance(session, Session)
    assert session.is_session is True
    assert session.open_et == OPEN_ET == time(9, 30)
    assert session.close_et == CLOSE_ET == time(16, 0)
    assert session.is_half_day is False
    assert session.closed_reason is None


@pytest.mark.parametrize("weekend", [date(2026, 6, 6), date(2026, 6, 7)])
def test_weekends_are_not_sessions(weekend):
    session = session_for(weekend)
    assert session.is_session is False
    assert session.closed_reason == "weekend"


def test_an_unknown_year_fails_closed():
    session = session_for(date(2025, 12, 31))
    assert session.is_session is False
    assert session.closed_reason == "calendar_out_of_range"


# --- half days -------------------------------------------------------------
@pytest.mark.parametrize(
    "half_day",
    [date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)],
)
def test_half_days_close_at_13_00(half_day):
    session = session_for(half_day)
    assert session.is_session is True
    assert session.is_half_day is True
    assert session.open_et == OPEN_ET
    assert session.close_et == HALF_DAY_CLOSE_ET == time(13, 0)


def test_july_third_2026_is_an_observed_holiday_not_a_half_day():
    session = session_for(date(2026, 7, 3))
    assert session.is_session is False
    assert session.closed_reason == "holiday"


def test_july_third_2027_falls_on_a_saturday():
    assert date(2027, 7, 3).weekday() == 5
    assert session_for(date(2027, 7, 3)).closed_reason == "weekend"


def test_a_regular_day_after_a_holiday_is_a_full_session():
    assert session_for(date(2026, 11, 30)).close_et == CLOSE_ET


# --- trading_days ----------------------------------------------------------
def test_trading_days_counts_january_2026_by_hand():
    # Jan 2026 has 22 weekdays; minus New Year (Thu 01-01) and MLK (Mon 01-19)
    # = 20. The span opens at 01-02, so New Year is already outside it.
    days = trading_days(date(2026, 1, 2), date(2026, 1, 31))
    assert len(days) == 20
    assert days[0] == date(2026, 1, 2)
    assert days[-1] == date(2026, 1, 30)
    assert date(2026, 1, 19) not in days
    assert all(day.weekday() < 5 for day in days)
    assert days == tuple(sorted(days))


def test_trading_days_includes_both_endpoints_when_they_are_sessions():
    days = trading_days(SESSION_DAY, date(2026, 6, 3))
    assert days == (date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3))


def test_an_inverted_or_unknown_span_is_empty():
    assert trading_days(date(2026, 6, 3), date(2026, 6, 1)) == ()
    assert trading_days(date(2025, 1, 1), date(2025, 1, 31)) == ()


# --- computus --------------------------------------------------------------
def test_computus_matches_known_easter_dates():
    assert easter_sunday(2026) == date(2026, 4, 5)
    assert easter_sunday(2027) == date(2027, 3, 28)
    assert easter_sunday(2024) == date(2024, 3, 31)


# --- session phase ---------------------------------------------------------
@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 29, "pre"),
        (9, 30, "rth"),
        (15, 59, "rth"),
        (16, 0, "post"),
        (18, 0, "post"),
    ],
)
def test_session_phase_reads_the_et_clock(hour, minute, expected):
    stamp = datetime.combine(SESSION_DAY, time(hour, minute), tzinfo=ET)
    assert session_phase(stamp) == expected


def test_session_phase_is_closed_on_a_weekend_and_before_a_holiday():
    weekend = datetime.combine(date(2026, 6, 6), time(12, 0), tzinfo=ET)
    assert session_phase(weekend) == "closed"
    holiday = datetime.combine(date(2026, 7, 3), time(12, 0), tzinfo=ET)
    assert session_phase(holiday) == "closed"


def test_session_phase_on_a_half_day_ends_at_13_00():
    before = datetime(2026, 11, 27, 12, 59, tzinfo=ET)
    after = datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    assert session_phase(before) == "rth"
    assert session_phase(after) == "post"


# --- ET <-> UTC conversion across the DST boundaries -----------------------
@pytest.mark.parametrize(
    ("naive_utc", "hour_utc"),
    [
        (datetime(2026, 3, 8, 12, 0), 8),   # 2026-03-08 is the spring-forward day
        (datetime(2026, 11, 1, 12, 0), 7),  # 2026-11-01 is the fall-back day
    ],
)
def test_to_et_applies_the_correct_dst_offset(naive_utc, hour_utc):
    et = to_et(naive_utc.replace(tzinfo=UTC))
    assert et.astimezone(ET).hour == hour_utc


def test_et_utc_round_trip_across_both_dst_boundaries():
    for local in (datetime(2026, 3, 8, 12, 0), datetime(2026, 11, 1, 12, 0)):
        aware = local.replace(tzinfo=ET)
        assert to_et(to_utc(aware)).replace(tzinfo=None) == local


def test_to_utc_uses_the_summer_offset_then_the_winter_offset():
    spring = to_utc(datetime(2026, 3, 8, 12, 0, tzinfo=ET))
    fall = to_utc(datetime(2026, 11, 1, 12, 0, tzinfo=ET))
    assert spring == datetime(2026, 3, 8, 16, 0, tzinfo=UTC)
    assert fall == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)


def test_naive_stamps_are_read_as_utc_on_input():
    assert to_et(datetime(2026, 6, 1, 13, 30)).astimezone(ET) == datetime(
        2026, 6, 1, 9, 30, tzinfo=ET
    )
    assert to_utc(datetime(2026, 6, 1, 9, 30)) == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)


# --- the documented tz fallback uses the same DST rule --------------------
@pytest.mark.parametrize(
    ("stamp", "offset_hours", "name"),
    [
        (datetime(2026, 1, 15, 12, 0), -5, "EST"),
        (datetime(2026, 7, 15, 12, 0), -4, "EDT"),
        (datetime(2026, 3, 8, 1, 59), -5, "EST"),
        (datetime(2026, 3, 8, 3, 0), -4, "EDT"),
        (datetime(2026, 11, 1, 1, 59), -4, "EDT"),
        (datetime(2026, 11, 1, 12, 0), -5, "EST"),
    ],
)
def test_fallback_tz_switches_on_the_us_rule(stamp, offset_hours, name):
    assert US_EASTERN.utcoffset(stamp) == timedelta(hours=offset_hours)
    assert US_EASTERN.tzname(stamp) == name


def test_conversions_use_the_fallback_when_it_is_selected(monkeypatch):
    monkeypatch.setattr(cal_module, "ET", US_EASTERN)
    assert to_utc(datetime(2026, 3, 8, 12, 0)) == datetime(2026, 3, 8, 16, 0, tzinfo=UTC)
    assert to_et(datetime(2026, 11, 1, 12, 0, tzinfo=UTC)).replace(tzinfo=None) == datetime(
        2026, 11, 1, 7, 0
    )
    assert session_phase(datetime(2026, 6, 1, 9, 30, tzinfo=US_EASTERN)) == "rth"


def test_fallback_tz_fromutc_matches_the_edt_start_instant():
    # 07:00 UTC on the spring-forward day is 03:00 EDT (02:00 local is skipped).
    spring = datetime(2026, 3, 8, 7, 0, tzinfo=UTC).astimezone(US_EASTERN)
    assert spring.replace(tzinfo=None) == datetime(2026, 3, 8, 3, 0)
    assert spring.utcoffset() == timedelta(hours=-4)
    fall = datetime(2026, 11, 1, 17, 0, tzinfo=UTC).astimezone(US_EASTERN)
    assert fall.replace(tzinfo=None) == datetime(2026, 11, 1, 12, 0)
    assert fall.utcoffset() == timedelta(hours=-5)
