"""Tests for the MCP datetime server's underlying tool functions."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

import server


# ---------------------------------------------------------------------------
# get_current_datetime
# ---------------------------------------------------------------------------

def test_get_current_datetime_keys_present():
    out = server.get_current_datetime()
    for key in ("iso8601", "date", "time", "timezone", "utc_offset", "weekday", "epoch_seconds"):
        assert key in out


def test_get_current_datetime_with_timezone():
    out = server.get_current_datetime("Asia/Tokyo")
    assert out["timezone"] == "Asia/Tokyo"
    assert out["utc_offset"] == "+09:00"


def test_get_current_datetime_unknown_tz():
    with pytest.raises(ValueError):
        server.get_current_datetime("Not/A_Real_Zone")


# ---------------------------------------------------------------------------
# convert_timezone
# ---------------------------------------------------------------------------

def test_convert_timezone_with_offset_input():
    out = server.convert_timezone("2026-05-18T09:30:00-04:00", "Asia/Tokyo")
    # 09:30 NYC EDT == 22:30 Tokyo same day
    assert out["to"]["iso8601"].startswith("2026-05-18T22:30:00")
    assert out["to"]["timezone"] == "Asia/Tokyo"


def test_convert_timezone_naive_with_from_tz():
    out = server.convert_timezone(
        "2026-05-18T09:30:00",
        to_timezone="Europe/London",
        from_timezone="America/New_York",
    )
    # 09:30 EDT == 14:30 BST
    assert out["to"]["iso8601"].startswith("2026-05-18T14:30:00")


# ---------------------------------------------------------------------------
# epoch round-trip
# ---------------------------------------------------------------------------

def test_to_epoch_then_from_epoch_round_trip():
    iso = "2026-05-18T13:46:40+00:00"
    e = server.to_epoch(iso)
    back = server.from_epoch(e["epoch_seconds"], timezone="UTC")
    assert back["iso8601"].startswith("2026-05-18T13:46:40")


def test_from_epoch_autodetect_milliseconds():
    out = server.from_epoch(1_747_576_000_000, timezone="UTC")
    assert out["detected_unit"] == "milliseconds"
    assert out["iso8601"].startswith("2025-")


def test_from_epoch_explicit_microseconds():
    out = server.from_epoch(1_747_576_000_000_000, timezone="UTC", unit="microseconds")
    assert out["detected_unit"] == "microseconds"


# ---------------------------------------------------------------------------
# parse_datetime
# ---------------------------------------------------------------------------

def test_parse_datetime_iso():
    out = server.parse_datetime("2026-05-18T09:30:00", timezone="UTC")
    assert out["parsed"]["iso8601"].startswith("2026-05-18T09:30:00")


@freeze_time("2026-05-18T12:00:00-04:00")
def test_parse_datetime_natural_language():
    out = server.parse_datetime("in 2 hours", timezone="America/New_York")
    assert out["parsed"]["iso8601"].startswith("2026-05-18T14:00")


# ---------------------------------------------------------------------------
# format_datetime
# ---------------------------------------------------------------------------

def test_format_datetime_rfc2822():
    out = server.format_datetime("2026-05-18T09:30:00+00:00", format="rfc2822")
    assert "May 2026" in out["formatted"]
    assert out["formatted"].endswith("+0000")


def test_format_datetime_strftime():
    out = server.format_datetime("2026-05-18T09:30:00+00:00", format="%Y/%m/%d")
    assert out["formatted"] == "2026/05/18"


def test_format_datetime_epoch_preset():
    out = server.format_datetime("2026-05-18T00:00:00+00:00", format="epoch")
    assert isinstance(out["formatted"], float)


# ---------------------------------------------------------------------------
# diff_datetimes / add_duration
# ---------------------------------------------------------------------------

def test_diff_datetimes_hours():
    out = server.diff_datetimes(
        "2026-05-18T09:00:00+00:00",
        "2026-05-18T17:30:00+00:00",
        unit="hours",
    )
    assert out["value"] == pytest.approx(8.5)


def test_add_duration_days_dst_safe():
    # America/New_York spring-forward 2026-03-08 (EST→EDT).
    # Adding 1 day to Mar 7 09:00 should land on Mar 8 09:00 local time,
    # i.e. preserve wall clock even though only 23 hours elapsed.
    out = server.add_duration(
        "2026-03-07T09:00:00",
        amount=1,
        unit="days",
        timezone="America/New_York",
    )
    assert out["output"]["iso8601"].startswith("2026-03-08T09:00:00")


def test_add_duration_negative_hours():
    out = server.add_duration(
        "2026-05-18T12:00:00+00:00",
        amount=-3,
        unit="hours",
    )
    assert out["output"]["iso8601"].startswith("2026-05-18T09:00:00")


def test_add_duration_months():
    out = server.add_duration(
        "2026-01-31T12:00:00+00:00",
        amount=1,
        unit="months",
    )
    # Jan 31 + 1 month = Feb 28 (relativedelta clamps)
    assert out["output"]["date"] == "2026-02-28"


# ---------------------------------------------------------------------------
# list_timezones
# ---------------------------------------------------------------------------

def test_list_timezones_query():
    out = server.list_timezones(query="tokyo")
    assert "Asia/Tokyo" in out["timezones"]


# ---------------------------------------------------------------------------
# market_status
# ---------------------------------------------------------------------------

def test_nyse_open_weekday_during_hours():
    # Monday 2026-05-18 10:30 ET — middle of regular session, not a holiday
    out = server.market_status("NYSE", at="2026-05-18T10:30:00-04:00")
    assert out["is_open"] is True
    assert out["session"] == "regular"


def test_nyse_closed_on_weekend():
    # Saturday
    out = server.market_status("NYSE", at="2026-05-16T10:30:00-04:00")
    assert out["is_open"] is False
    assert out["is_business_day"] is False


def test_nyse_closed_on_holiday_independence_day_observed():
    # July 4, 2026 falls on a Saturday → NYSE observes on Friday July 3
    out = server.market_status("NYSE", at="2026-07-03T10:30:00-04:00")
    assert out["is_open"] is False
    assert out["is_business_day"] is False


def test_crypto_always_open():
    out = server.market_status("CRYPTO", at="2026-05-16T03:00:00+00:00")
    assert out["is_open"] is True


def test_unknown_market_raises():
    with pytest.raises(ValueError):
        server.market_status("FOO")


# ---------------------------------------------------------------------------
# business_days / add_business_days
# ---------------------------------------------------------------------------

def test_business_days_simple_week():
    # Mon 2026-05-18 → Mon 2026-05-25, no holidays in this span
    out = server.business_days("2026-05-18", "2026-05-25")
    assert out["business_days"] == 5


def test_add_business_days_skips_weekend():
    # Friday 2026-06-05 + 1 business day = Monday 2026-06-08 (no US holidays in span)
    out = server.add_business_days("2026-06-05", 1)
    assert out["output"] == "2026-06-08"


def test_add_business_days_skips_us_holiday():
    # Friday 2026-05-22, Memorial Day is Mon May 25 → +1 business day = Tue May 26
    out = server.add_business_days("2026-05-22", 1)
    assert out["output"] == "2026-05-26"


# ---------------------------------------------------------------------------
# calendar_info
# ---------------------------------------------------------------------------

def test_calendar_info_known_date():
    out = server.calendar_info("2026-05-18")
    assert out["weekday"] == "Monday"
    assert out["quarter"] == 2
    assert out["iso_week"] == 21
    assert out["is_leap_year"] is False
    assert out["days_in_month"] == 31


def test_calendar_info_leap_year():
    out = server.calendar_info("2028-02-15")
    assert out["is_leap_year"] is True
    assert out["days_in_month"] == 29


# ---------------------------------------------------------------------------
# humanize_duration
# ---------------------------------------------------------------------------

def test_humanize_long():
    out = server.humanize_duration(93_824)
    assert out["human"] == "1 day, 2 hours, 3 minutes, 44 seconds"


def test_humanize_compact():
    out = server.humanize_duration(93_824, style="compact")
    assert out["human"] == "1d 2h 3m 44s"


def test_humanize_negative():
    out = server.humanize_duration(-3600)
    assert out["human"].startswith("-")


# ---------------------------------------------------------------------------
# time_until / time_since
# ---------------------------------------------------------------------------

@freeze_time("2026-05-18T12:00:00+00:00")
def test_time_until_future():
    out = server.time_until("2026-05-18T13:00:00+00:00")
    assert out["in_future"] is True
    assert out["seconds"] == pytest.approx(3600)


@freeze_time("2026-05-18T12:00:00+00:00")
def test_time_since_past():
    out = server.time_since("2026-05-18T10:00:00+00:00")
    assert out["in_past"] is True
    assert out["seconds"] == pytest.approx(7200)
