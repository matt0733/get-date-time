"""MCP server exposing date, time, timezone, calendar, and market-session tools.

Designed for local LLM hosts (LM Studio, Claude Desktop, etc.) that speak MCP
over stdio. Every datetime is treated as timezone-aware whenever a timezone is
known or can be inferred. All math is DST-correct.
"""

from __future__ import annotations

import calendar as _calendar
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import holidays as _holidays
import parsedatetime as _pdt
from dateutil import parser as _dateparser
from dateutil.relativedelta import relativedelta
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("datetime")

_DURATION_UNITS = {
    "seconds", "second", "secs", "sec", "s",
    "minutes", "minute", "mins", "min", "m",
    "hours", "hour", "hrs", "hr", "h",
    "days", "day", "d",
    "weeks", "week", "w",
    "months", "month", "mo",
    "years", "year", "y",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_tz(name: str | None) -> ZoneInfo | None:
    if name is None or name == "":
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as err:
        raise ValueError(f"Unknown timezone: {name!r}") from err


def _format_offset(offset: timedelta | None) -> str:
    if offset is None:
        return "+00:00"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def _to_aware(dt: datetime, fallback_tz: ZoneInfo | None) -> datetime:
    """Attach fallback_tz to a naive datetime; leave aware datetimes untouched."""
    if dt.tzinfo is None:
        tz = fallback_tz or datetime.now().astimezone().tzinfo
        return dt.replace(tzinfo=tz)
    return dt


def _parse_iso(text: str) -> datetime:
    text = text.strip()
    try:
        return _dateparser.isoparse(text)
    except (ValueError, TypeError):
        return _dateparser.parse(text)


def _datetime_payload(dt: datetime) -> dict[str, Any]:
    offset = dt.utcoffset()
    tzname = str(dt.tzinfo) if dt.tzinfo is not None else "naive"
    return {
        "iso8601": dt.isoformat(),
        "date": dt.date().isoformat(),
        "time": dt.time().isoformat(timespec="seconds"),
        "timezone": tzname,
        "utc_offset": _format_offset(offset),
        "weekday": dt.strftime("%A"),
        "epoch_seconds": dt.timestamp() if dt.tzinfo is not None else None,
    }


def _normalize_unit(unit: str) -> str:
    u = unit.strip().lower()
    if u not in _DURATION_UNITS:
        raise ValueError(
            f"Unknown duration unit: {unit!r}. "
            "Use seconds, minutes, hours, days, weeks, months, or years."
        )
    if u in {"s", "sec", "secs", "second", "seconds"}:
        return "seconds"
    if u in {"m", "min", "mins", "minute", "minutes"}:
        return "minutes"
    if u in {"h", "hr", "hrs", "hour", "hours"}:
        return "hours"
    if u in {"d", "day", "days"}:
        return "days"
    if u in {"w", "week", "weeks"}:
        return "weeks"
    if u in {"mo", "month", "months"}:
        return "months"
    return "years"


def _humanize(total_seconds: float, style: str = "long") -> str:
    sign = "-" if total_seconds < 0 else ""
    total = int(abs(total_seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if style == "compact":
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds or not parts:
            parts.append(f"{seconds}s")
        return sign + " ".join(parts)
    # long form
    labels = [("day", days), ("hour", hours), ("minute", minutes), ("second", seconds)]
    for label, value in labels:
        if value:
            parts.append(f"{value} {label}{'s' if value != 1 else ''}")
    if not parts:
        parts.append("0 seconds")
    return sign + ", ".join(parts)


# ---------------------------------------------------------------------------
# Market session schedule
# ---------------------------------------------------------------------------

_MARKETS: dict[str, dict[str, Any]] = {
    "NYSE":   {"tz": "America/New_York", "sessions": [("09:30", "16:00")], "weekdays": (0, 1, 2, 3, 4), "holidays": ("US", "NYSE")},
    "NASDAQ": {"tz": "America/New_York", "sessions": [("09:30", "16:00")], "weekdays": (0, 1, 2, 3, 4), "holidays": ("US", "NYSE")},
    "LSE":    {"tz": "Europe/London",    "sessions": [("08:00", "16:30")], "weekdays": (0, 1, 2, 3, 4), "holidays": ("GB",)},
    "TSE":    {"tz": "Asia/Tokyo",       "sessions": [("09:00", "11:30"), ("12:30", "15:00")], "weekdays": (0, 1, 2, 3, 4), "holidays": ("JP",)},
    "HKEX":   {"tz": "Asia/Hong_Kong",   "sessions": [("09:30", "12:00"), ("13:00", "16:00")], "weekdays": (0, 1, 2, 3, 4), "holidays": ("HK",)},
    "CME":    {"tz": "America/Chicago",  "sessions": [("17:00+prev", "16:00")], "weekdays": (0, 1, 2, 3, 4), "holidays": ("US", "NYSE"), "weekly_close": ("Fri", "16:00", "Sun", "17:00")},
    "CRYPTO": {"tz": "UTC",              "sessions": [("00:00", "24:00")], "weekdays": (0, 1, 2, 3, 4, 5, 6), "holidays": ()},
}


def _market_holiday_set(spec: tuple[str, ...], years: list[int]) -> set[date]:
    out: set[date] = set()
    for code in spec:
        if code == "NYSE":
            try:
                cal = _holidays.financial_holidays("NYSE", years=years)
                out.update(cal.keys())
            except (KeyError, NotImplementedError):
                pass
        else:
            try:
                cal = _holidays.country_holidays(code, years=years)
                out.update(cal.keys())
            except (KeyError, NotImplementedError):
                pass
    return out


def _parse_hhmm(text: str) -> time:
    if text == "24:00":
        return time(23, 59, 59)
    hh, mm = text.split(":")
    return time(int(hh), int(mm))


def _market_status_at(market_key: str, now: datetime) -> dict[str, Any]:
    cfg = _MARKETS[market_key]
    tz = ZoneInfo(cfg["tz"])
    local = now.astimezone(tz)
    today = local.date()
    holidays_set = _market_holiday_set(tuple(cfg["holidays"]), [today.year, today.year + 1])

    # crypto is always open
    if market_key == "CRYPTO":
        return {
            "market": market_key,
            "timezone": str(tz),
            "is_open": True,
            "session": "24x7",
            "current_local_time": local.isoformat(),
            "next_open": None,
            "next_close": None,
        }

    is_open = False
    session_label = "closed"
    is_business_day = local.weekday() in cfg["weekdays"] and today not in holidays_set
    if is_business_day:
        for start_s, end_s in cfg["sessions"]:
            if "+prev" in start_s:
                # CME-style continuous session — simplified
                if local.weekday() == 4 and local.time() >= _parse_hhmm("16:00"):
                    is_open = False
                else:
                    is_open = True
                session_label = "regular"
                break
            start_t = _parse_hhmm(start_s)
            end_t = _parse_hhmm(end_s)
            if start_t <= local.time() < end_t:
                is_open = True
                session_label = "regular"
                break

    # find next open and next close by scanning forward
    next_open: datetime | None = None
    next_close: datetime | None = None
    scan = local
    for _ in range(14):  # look ahead two weeks
        d = scan.date()
        if d.weekday() in cfg["weekdays"] and d not in holidays_set:
            for start_s, end_s in cfg["sessions"]:
                if "+prev" in start_s:
                    continue  # CME special-case handled below
                open_dt = datetime.combine(d, _parse_hhmm(start_s), tzinfo=tz)
                close_dt = datetime.combine(d, _parse_hhmm(end_s), tzinfo=tz)
                if not is_open and next_open is None and open_dt > local:
                    next_open = open_dt
                if is_open and next_close is None and close_dt > local:
                    next_close = close_dt
        scan = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=tz)
        if next_open and (next_close or not is_open):
            break

    return {
        "market": market_key,
        "timezone": str(tz),
        "is_open": is_open,
        "is_business_day": is_business_day,
        "session": session_label,
        "current_local_time": local.isoformat(),
        "next_open": next_open.isoformat() if next_open else None,
        "next_close": next_close.isoformat() if next_close else None,
    }


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_current_datetime(timezone: str | None = None) -> dict:
    """Return the current date, time, and timezone.

    Args:
        timezone: Optional IANA timezone name (e.g. "America/Los_Angeles",
            "Europe/London", "UTC"). If omitted, the host machine's local
            timezone is used.

    Returns ISO-8601 datetime, date, time, timezone name, UTC offset, weekday,
    and Unix epoch seconds.
    """
    tz = _resolve_tz(timezone)
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    return _datetime_payload(now)


@mcp.tool()
def convert_timezone(datetime_str: str, to_timezone: str, from_timezone: str | None = None) -> dict:
    """Convert an ISO-8601 datetime (or one with embedded offset) into another timezone.

    Args:
        datetime_str: ISO-8601 datetime (e.g. "2026-05-18T09:30:00" or
            "2026-05-18T09:30:00-04:00"). Bare dates/times also accepted.
        to_timezone: Target IANA timezone name.
        from_timezone: IANA timezone of the input if it is naive (no offset).
            Ignored when the input already carries an offset.
    """
    dt = _parse_iso(datetime_str)
    src_tz = _resolve_tz(from_timezone)
    dst_tz = _resolve_tz(to_timezone)
    if dst_tz is None:
        raise ValueError("to_timezone is required")
    aware = _to_aware(dt, src_tz)
    converted = aware.astimezone(dst_tz)
    return {
        "from": _datetime_payload(aware),
        "to": _datetime_payload(converted),
    }


@mcp.tool()
def to_epoch(datetime_str: str, timezone: str | None = None) -> dict:
    """Convert an ISO-8601 datetime to a Unix epoch timestamp.

    Args:
        datetime_str: ISO-8601 datetime; bare dates assumed at 00:00.
        timezone: IANA timezone used if the input is naive (no offset).

    Returns epoch in seconds, milliseconds, and microseconds.
    """
    dt = _parse_iso(datetime_str)
    tz = _resolve_tz(timezone)
    aware = _to_aware(dt, tz)
    secs = aware.timestamp()
    return {
        "iso8601": aware.isoformat(),
        "epoch_seconds": secs,
        "epoch_milliseconds": int(secs * 1000),
        "epoch_microseconds": int(secs * 1_000_000),
    }


@mcp.tool()
def from_epoch(value: float, timezone: str | None = None, unit: str | None = None) -> dict:
    """Convert a Unix epoch timestamp to a human-readable datetime.

    Args:
        value: Numeric epoch timestamp. Unit is auto-detected unless overridden.
        timezone: IANA timezone for the output. Defaults to host local time.
        unit: One of "seconds", "milliseconds", "microseconds", "nanoseconds".
            If omitted, the magnitude of `value` determines the unit
            (s < 1e11, ms < 1e14, us < 1e17, otherwise ns).
    """
    if unit:
        u = unit.strip().lower()
        divisor = {"seconds": 1.0, "milliseconds": 1_000.0, "microseconds": 1_000_000.0, "nanoseconds": 1_000_000_000.0}.get(u)
        if divisor is None:
            raise ValueError(f"Unknown epoch unit: {unit!r}")
        seconds = value / divisor
        detected = u
    else:
        abs_v = abs(value)
        if abs_v < 1e11:
            seconds, detected = value, "seconds"
        elif abs_v < 1e14:
            seconds, detected = value / 1_000.0, "milliseconds"
        elif abs_v < 1e17:
            seconds, detected = value / 1_000_000.0, "microseconds"
        else:
            seconds, detected = value / 1_000_000_000.0, "nanoseconds"
    tz = _resolve_tz(timezone) or datetime.now().astimezone().tzinfo
    dt = datetime.fromtimestamp(seconds, tz=tz)
    payload = _datetime_payload(dt)
    payload["detected_unit"] = detected
    return payload


@mcp.tool()
def parse_datetime(text: str, timezone: str | None = None, reference: str | None = None) -> dict:
    """Parse a natural-language or ISO-8601 datetime string.

    Handles ISO-8601, RFC 2822, and natural language like "next Friday 3pm",
    "in 2 hours", "tomorrow at noon", "last Monday".

    Args:
        text: Datetime string to parse.
        timezone: IANA timezone to interpret the result in (defaults to local).
        reference: Optional ISO-8601 reference moment used as "now" when the
            input is relative ("in 2 hours"). Defaults to the current time.
    """
    tz = _resolve_tz(timezone) or datetime.now().astimezone().tzinfo
    ref_dt = _parse_iso(reference) if reference else datetime.now(tz)
    if ref_dt.tzinfo is None:
        ref_dt = ref_dt.replace(tzinfo=tz)
    # try strict ISO first
    try:
        dt = _dateparser.parse(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return {"parsed": _datetime_payload(dt), "method": "dateutil"}
    except (ValueError, TypeError, OverflowError):
        pass
    cal = _pdt.Calendar()
    time_struct, parse_status = cal.parse(text, sourceTime=ref_dt.timetuple())
    if parse_status == 0:
        raise ValueError(f"Could not parse: {text!r}")
    dt = datetime(*time_struct[:6], tzinfo=tz)
    return {"parsed": _datetime_payload(dt), "method": "parsedatetime"}


@mcp.tool()
def format_datetime(datetime_str: str, format: str = "rfc3339", timezone: str | None = None) -> dict:
    """Format a datetime using a preset name or strftime pattern.

    Args:
        datetime_str: ISO-8601 datetime to format.
        format: One of the presets {"rfc3339", "rfc2822", "iso8601", "epoch",
            "epoch_ms", "human", "date", "time"}, or a strftime pattern like
            "%Y-%m-%d %H:%M:%S".
        timezone: Optional IANA timezone to convert to before formatting.
    """
    dt = _parse_iso(datetime_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    if timezone:
        dt = dt.astimezone(_resolve_tz(timezone))
    presets = {
        "rfc3339": lambda d: d.isoformat(),
        "iso8601": lambda d: d.isoformat(),
        "rfc2822": lambda d: d.strftime("%a, %d %b %Y %H:%M:%S %z"),
        "epoch": lambda d: d.timestamp(),
        "epoch_ms": lambda d: int(d.timestamp() * 1000),
        "human": lambda d: d.strftime("%A, %B %-d, %Y at %-I:%M %p %Z").strip(),
        "date": lambda d: d.date().isoformat(),
        "time": lambda d: d.time().isoformat(timespec="seconds"),
    }
    fmt = format.strip()
    if fmt in presets:
        return {"formatted": presets[fmt](dt), "format": fmt}
    return {"formatted": dt.strftime(fmt), "format": "strftime"}


@mcp.tool()
def diff_datetimes(start: str, end: str, unit: str = "seconds") -> dict:
    """Compute the difference between two datetimes (end - start).

    For whole-day counts between two calendar dates (e.g. "how many days from
    today until Friday"), prefer `days_between` — this tool does timestamp
    subtraction and will return fractional days when partial days are
    involved.

    Args:
        start: ISO-8601 datetime.
        end: ISO-8601 datetime.
        unit: Output unit: seconds, minutes, hours, days, weeks, months, years.

    Returns the difference in the requested unit, total seconds, and a
    human-readable string. Month/year results use a calendar-aware delta.
    """
    a = _parse_iso(start)
    b = _parse_iso(end)
    a = _to_aware(a, None)
    b = _to_aware(b, None)
    delta_seconds = (b - a).total_seconds()
    u = _normalize_unit(unit)
    factor = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800}
    if u in factor:
        value = delta_seconds / factor[u]
    else:
        rd = relativedelta(b, a)
        if u == "months":
            value = rd.years * 12 + rd.months + rd.days / 30.4375
        else:  # years
            value = rd.years + rd.months / 12 + rd.days / 365.25
    return {
        "start": a.isoformat(),
        "end": b.isoformat(),
        "value": value,
        "unit": u,
        "total_seconds": delta_seconds,
        "human": _humanize(delta_seconds),
    }


@mcp.tool()
def add_duration(datetime_str: str, amount: float, unit: str, timezone: str | None = None) -> dict:
    """Add (or subtract) a duration to a datetime, DST-correctly.

    Args:
        datetime_str: ISO-8601 datetime.
        amount: Quantity to add. Use a negative number to subtract.
        unit: seconds, minutes, hours, days, weeks, months, years.
        timezone: IANA timezone for the input if it is naive.
    """
    dt = _parse_iso(datetime_str)
    tz = _resolve_tz(timezone)
    dt = _to_aware(dt, tz)
    u = _normalize_unit(unit)
    if u in {"seconds", "minutes", "hours"}:
        seconds = {"seconds": 1, "minutes": 60, "hours": 3600}[u] * amount
        result = dt + timedelta(seconds=seconds)
    elif u in {"days", "weeks"}:
        # calendar-aware so DST transitions land on the same wall-clock time
        kwarg = {u: int(amount)} if amount.is_integer() else {"days": amount * (7 if u == "weeks" else 1)}
        if amount.is_integer():
            result = dt + relativedelta(**kwarg)
        else:
            result = dt + timedelta(**kwarg)
    elif u == "months":
        if not float(amount).is_integer():
            raise ValueError("Months must be a whole number")
        result = dt + relativedelta(months=int(amount))
    else:  # years
        if not float(amount).is_integer():
            raise ValueError("Years must be a whole number")
        result = dt + relativedelta(years=int(amount))
    return {
        "input": _datetime_payload(dt),
        "output": _datetime_payload(result),
        "amount": amount,
        "unit": u,
    }


@mcp.tool()
def days_between(start_date: str, end_date: str, inclusive: bool = False) -> dict:
    """Count whole calendar days between two dates (end - start), ignoring time of day.

    Use this for questions like "how many days until Friday" or "how many
    days between May 18 and May 29". Always returns an integer; never
    contaminated by the current time of day.

    Args:
        start_date: ISO-8601 date (YYYY-MM-DD). Times are stripped if present.
        end_date: ISO-8601 date (YYYY-MM-DD). Times are stripped if present.
        inclusive: If True, count both endpoints (adds 1). Default False —
            so days_between("2026-05-18", "2026-05-29") = 11, the gap.
    """
    s = _parse_iso(start_date).date()
    e = _parse_iso(end_date).date()
    delta = (e - s).days
    count = delta + (1 if inclusive and delta >= 0 else (-1 if inclusive and delta < 0 else 0))
    return {
        "start_date": s.isoformat(),
        "end_date": e.isoformat(),
        "days": count,
        "inclusive": inclusive,
        "direction": "forward" if delta >= 0 else "backward",
    }


@mcp.tool()
def list_timezones(query: str | None = None, limit: int = 50) -> dict:
    """List IANA timezones, optionally filtered by a substring.

    Args:
        query: Case-insensitive substring to match against zone names.
        limit: Maximum names to return (default 50, max 500).
    """
    limit = max(1, min(int(limit), 500))
    names = sorted(available_timezones())
    if query:
        q = query.lower()
        names = [n for n in names if q in n.lower()]
    return {
        "count": len(names),
        "returned": min(limit, len(names)),
        "timezones": names[:limit],
    }


@mcp.tool()
def market_status(market: str, at: str | None = None) -> dict:
    """Report whether a financial market is currently open.

    Args:
        market: One of "NYSE", "NASDAQ", "LSE" (London), "TSE" (Tokyo),
            "HKEX" (Hong Kong), "CME" (E-mini futures, simplified), "CRYPTO".
        at: Optional ISO-8601 datetime to evaluate (defaults to now).

    Honours weekends and known holidays. Does not currently model exchange
    early-close days. CME is approximated as continuous Mon-Fri with Friday
    16:00 CT close.
    """
    key = market.strip().upper()
    if key not in _MARKETS:
        raise ValueError(f"Unknown market: {market!r}. Supported: {sorted(_MARKETS)}")
    now = _parse_iso(at) if at else datetime.now().astimezone()
    now = _to_aware(now, None)
    return _market_status_at(key, now)


@mcp.tool()
def business_days(start: str, end: str, country: str = "US") -> dict:
    """Count business days (Mon-Fri minus holidays) between two dates, inclusive of start, exclusive of end.

    Args:
        start: ISO-8601 date (YYYY-MM-DD).
        end: ISO-8601 date (YYYY-MM-DD).
        country: ISO country code for the holiday calendar (US, GB, JP, HK, CA, DE, ...).
    """
    s = _parse_iso(start).date()
    e = _parse_iso(end).date()
    direction = 1 if e >= s else -1
    years = list(range(min(s, e).year, max(s, e).year + 1))
    try:
        cal = _holidays.country_holidays(country, years=years)
    except (KeyError, NotImplementedError) as err:
        raise ValueError(f"Unknown country code for holidays: {country!r}") from err
    count = 0
    cur = s
    while cur != e:
        if cur.weekday() < 5 and cur not in cal:
            count += 1
        cur = cur + timedelta(days=direction)
    return {
        "start": s.isoformat(),
        "end": e.isoformat(),
        "business_days": count * direction,
        "country": country,
    }


@mcp.tool()
def add_business_days(date_str: str, days: int, country: str = "US") -> dict:
    """Add (or subtract, with negative) N business days to a date.

    Args:
        date_str: ISO-8601 date.
        days: Number of business days to advance. Negative goes backward.
        country: ISO country code for the holiday calendar.
    """
    d = _parse_iso(date_str).date()
    step = 1 if days >= 0 else -1
    remaining = abs(days)
    years = [d.year, d.year + step]
    try:
        cal = _holidays.country_holidays(country, years=years + [d.year + 2 * step])
    except (KeyError, NotImplementedError) as err:
        raise ValueError(f"Unknown country code for holidays: {country!r}") from err
    cur = d
    while remaining > 0:
        cur = cur + timedelta(days=step)
        if cur.weekday() < 5 and cur not in cal:
            remaining -= 1
    return {"input": d.isoformat(), "output": cur.isoformat(), "days_added": days, "country": country}


@mcp.tool()
def calendar_info(date_str: str) -> dict:
    """Return calendar facts about a date.

    Args:
        date_str: ISO-8601 date (YYYY-MM-DD).

    Includes ISO week number/year, quarter, day-of-year, day-of-week,
    is_leap_year, days_in_month, days_remaining_in_month, days_remaining_in_year.
    """
    d = _parse_iso(date_str).date()
    iso_year, iso_week, iso_weekday = d.isocalendar()
    days_in_month = _calendar.monthrange(d.year, d.month)[1]
    last_of_year = date(d.year, 12, 31)
    return {
        "date": d.isoformat(),
        "weekday": d.strftime("%A"),
        "iso_year": iso_year,
        "iso_week": iso_week,
        "iso_weekday": iso_weekday,  # 1 = Monday
        "quarter": (d.month - 1) // 3 + 1,
        "day_of_year": d.timetuple().tm_yday,
        "days_in_month": days_in_month,
        "days_remaining_in_month": days_in_month - d.day,
        "days_remaining_in_year": (last_of_year - d).days,
        "is_leap_year": _calendar.isleap(d.year),
        "month_name": d.strftime("%B"),
    }


@mcp.tool()
def humanize_duration(seconds: float, style: str = "long") -> dict:
    """Render a duration in seconds as a human-readable string.

    Args:
        seconds: Total seconds.
        style: "long" → "1 day, 2 hours, 3 minutes" or "compact" → "1d 2h 3m".
    """
    s = style.strip().lower()
    if s not in {"long", "compact"}:
        raise ValueError("style must be 'long' or 'compact'")
    return {"seconds": seconds, "style": s, "human": _humanize(seconds, s)}


@mcp.tool()
def time_until(target: str, timezone: str | None = None) -> dict:
    """How long from now until the given datetime.

    Args:
        target: ISO-8601 datetime.
        timezone: IANA timezone for the target if it is naive (defaults local).
    """
    tz = _resolve_tz(timezone)
    target_dt = _to_aware(_parse_iso(target), tz)
    now = datetime.now(target_dt.tzinfo)
    delta = (target_dt - now).total_seconds()
    return {
        "target": target_dt.isoformat(),
        "now": now.isoformat(),
        "seconds": delta,
        "in_future": delta >= 0,
        "human": _humanize(delta),
    }


@mcp.tool()
def time_since(reference: str, timezone: str | None = None) -> dict:
    """How long has passed since the given datetime.

    Args:
        reference: ISO-8601 datetime in the past.
        timezone: IANA timezone for the input if it is naive (defaults local).
    """
    tz = _resolve_tz(timezone)
    ref_dt = _to_aware(_parse_iso(reference), tz)
    now = datetime.now(ref_dt.tzinfo)
    delta = (now - ref_dt).total_seconds()
    return {
        "reference": ref_dt.isoformat(),
        "now": now.isoformat(),
        "seconds": delta,
        "in_past": delta >= 0,
        "human": _humanize(delta),
    }


if __name__ == "__main__":
    mcp.run()
