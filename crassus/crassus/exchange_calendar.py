"""NYSE holiday and early-close calendar.

Primary source is `pandas_market_calendars`'s NYSE calendar -- the same
library `backfill_options_history.py` already depends on elsewhere in this
repo -- because it carries NYSE's actual published early closes (e.g. the
day before July 4th in years the exchange announces one), not just the two
recurring occasions (day after Thanksgiving, Christmas Eve). A hand-rolled
rule set was tried first to avoid pulling pandas into crassus's requirements,
but review caught that it silently missed any early close outside those two
categories (flagged case: 2028-07-03) -- an EOD flatten computing the wrong
close time is exactly the kind of bug this module exists to prevent, so
correctness wins over the dependency-weight tradeoff here.

The original hand-rolled rules are kept as `_fallback_session_close` /
`_fallback_is_holiday`, used only if `pandas_market_calendars` fails to
import or raises -- so a broken/missing calendar package degrades to the
old (narrower but still correct-for-2-of-N-cases) behavior instead of
crashing `flatten.maybe_flatten` outright.

`session_phase` in clock.py already tolerates holidays reading as `open`
(the server's stale-quote rejection is its backstop); this module only
answers the early-close question the mandatory EOD flatten actually depends
on -- a half day must use its real close, not an unconditional 16:00.
"""

from __future__ import annotations

import logging
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

_nyse_schedule_cache: dict[int, object] = {}  # year -> pandas DataFrame, populated lazily


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth (1-indexed) occurrence of `weekday` (Mon=0) in a month."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d = next_month_first - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _good_friday(year: int) -> date:
    """Anonymous Gregorian (Meeus/Jones/Butcher) algorithm for Easter Sunday,
    two days before which NYSE always closes."""
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
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)


def _observed(d: date) -> date:
    """A fixed-date holiday observed on the nearest weekday (Sat->Fri, Sun->Mon)."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # MLK Day
        _nth_weekday(year, 2, 0, 3),  # Presidents Day
        _good_friday(year),
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    if year >= 2022:  # first NYSE-observed Juneteenth
        holidays.add(_observed(date(year, 6, 19)))
    return holidays


def _early_closes(year: int) -> set[date]:
    """Day after Thanksgiving, and Christmas Eve when it's itself a trading
    day (not a weekend or, if Dec 25 falls on Saturday, the observed-Friday
    Christmas holiday)."""
    day_after_thanksgiving = _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    closes = {day_after_thanksgiving}
    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5 and christmas_eve not in _holidays(year):
        closes.add(christmas_eve)
    return closes


_holiday_cache: dict[int, set[date]] = {}
_early_close_cache: dict[int, set[date]] = {}


def _fallback_is_holiday(d: date) -> bool:
    if d.year not in _holiday_cache:
        _holiday_cache[d.year] = _holidays(d.year)
    return d in _holiday_cache[d.year]


def _fallback_session_close(d: date) -> time:
    """Day after Thanksgiving and Christmas Eve only -- see module docstring
    for why this is a fallback, not the primary path."""
    if d.year not in _early_close_cache:
        _early_close_cache[d.year] = _early_closes(d.year)
    return EARLY_CLOSE if d in _early_close_cache[d.year] else MARKET_CLOSE


def _nyse_schedule_for_year(year: int):
    """NYSE's published full-year schedule (open/close timestamps per
    trading day), cached per year. Returns `None` if `pandas_market_calendars`
    isn't importable or raises -- callers fall back to the hand-rolled rules.
    """
    if year in _nyse_schedule_cache:
        return _nyse_schedule_cache[year]
    try:
        import pandas_market_calendars as mcal

        schedule = mcal.get_calendar("NYSE").schedule(
            start_date=date(year, 1, 1), end_date=date(year, 12, 31)
        )
    except Exception:
        log.warning(
            "pandas_market_calendars unavailable/failed for %d -- falling back to the "
            "hand-rolled holiday/early-close rules (Thanksgiving Friday + Christmas Eve only)",
            year, exc_info=True,
        )
        schedule = None
    _nyse_schedule_cache[year] = schedule
    return schedule


def is_holiday(d: date) -> bool:
    schedule = _nyse_schedule_for_year(d.year)
    if schedule is None:
        return _fallback_is_holiday(d)
    return date(d.year, d.month, d.day) not in schedule.index.date


def session_close(d: date) -> time:
    """The regular session's closing time on `d` -- 16:00 ET normally, or
    whatever earlier time NYSE has actually published for a half day (day
    after Thanksgiving, Christmas Eve, and any other announced early close,
    e.g. the day before July 4th in years NYSE closes early for it).

    Does not itself check `is_holiday` -- callers already route full-holiday
    closures through `session_phase`'s open/closed determination; this only
    answers how early *today* closes, given that it's already known to be a
    trading day.
    """
    schedule = _nyse_schedule_for_year(d.year)
    if schedule is None:
        return _fallback_session_close(d)
    try:
        row = schedule.loc[schedule.index.date == d]
    except Exception:
        return _fallback_session_close(d)
    if row.empty:
        # Not a trading day per the real calendar either -- callers already
        # guard on session_phase before reaching here, so this is defensive,
        # not expected to be hit in practice.
        return MARKET_CLOSE
    close_ts = row["market_close"].iloc[0]
    return close_ts.tz_convert(ET).time()
