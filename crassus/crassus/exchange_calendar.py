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

`is_holiday`'s original hand-rolled rules are kept as `_fallback_is_holiday`,
used only if `pandas_market_calendars` fails to import or raises. This isn't
the close-safety-critical path -- `session_phase` in clock.py already
tolerates a holiday reading as `open`, backstopped by the server's own
stale-quote rejection -- so degrading to the old rules here is acceptable.

`session_close` is different: it's what `flatten.maybe_flatten` -- a
mandatory, not opt-in, close-safety control -- uses to decide when the EOD
flatten window opens. An earlier version fell back to the same hand-rolled
early-close rules here too, but review correctly pointed out that's not
fail-safe: those rules have the exact defect this module exists to fix
(missing early closes outside two hard-coded categories), so falling back
to them on a calendar failure could silently restore the wrong close time
on precisely the kind of unmodeled half day this module is meant to catch.
On any failure to read the real calendar, `session_close` now returns
`EARLY_CLOSE` unconditionally -- the conservative direction for a mandatory
flatten is to open the window too early on an ordinary day (costs some
runway, not safety) rather than risk opening it too late on a real half
day it can't verify.

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


_holiday_cache: dict[int, set[date]] = {}


def _fallback_is_holiday(d: date) -> bool:
    if d.year not in _holiday_cache:
        _holiday_cache[d.year] = _holidays(d.year)
    return d in _holiday_cache[d.year]


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
            "pandas_market_calendars unavailable/failed for %d -- is_holiday() falls back to "
            "the hand-rolled holiday rules, but session_close() will conservatively return "
            "EARLY_CLOSE for every day this year rather than risk an incorrect 16:00 on an "
            "unmodeled half day (this is a mandatory close-safety control -- see module docstring)",
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
        return EARLY_CLOSE  # conservative: see module docstring
    try:
        row = schedule.loc[schedule.index.date == d]
    except Exception:
        return EARLY_CLOSE  # conservative: see module docstring
    if row.empty:
        # Not a trading day per the real calendar either -- callers already
        # guard on session_phase before reaching here, so this is defensive,
        # not expected to be hit in practice.
        return MARKET_CLOSE
    close_ts = row["market_close"].iloc[0]
    return close_ts.tz_convert(ET).time()
