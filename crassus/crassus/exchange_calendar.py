"""Minimal NYSE holiday and early-close calendar.

Hand-rolled rather than pulling in `pandas_market_calendars` (pandas plus a
calendar library) as a dependency: this repo's requirements stay deliberately
small (see requirements.txt), and all `flatten.maybe_flatten` needs is "what
time does the regular session close today" -- not a general trading-calendar
library.

`session_phase` in clock.py already tolerates holidays reading as `open`
(the server's stale-quote rejection is its backstop); this module only
answers the early-close question the mandatory EOD flatten actually depends
on -- a half day must use its real close, not an unconditional 16:00.
"""

from __future__ import annotations

from datetime import date, time, timedelta

MARKET_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


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


def is_holiday(d: date) -> bool:
    if d.year not in _holiday_cache:
        _holiday_cache[d.year] = _holidays(d.year)
    return d in _holiday_cache[d.year]


def session_close(d: date) -> time:
    """The regular session's closing time on `d` -- 16:00 ET normally, 13:00
    ET on the day after Thanksgiving and (when it's a trading day) Christmas
    Eve.

    Does not itself check `is_holiday` -- callers already route full-holiday
    closures through `session_phase`'s open/closed determination; this only
    answers how early *today* closes, given that it's already known to be a
    trading day.
    """
    if d.year not in _early_close_cache:
        _early_close_cache[d.year] = _early_closes(d.year)
    return EARLY_CLOSE if d in _early_close_cache[d.year] else MARKET_CLOSE
