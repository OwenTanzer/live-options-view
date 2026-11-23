"""Explicit price-lane session model, separate from the option-chain lifecyle.

WHY this module: the collector's original session guard answers one question --
"may we authenticate for the QQQ/SPY chain?" -- and the price strip inherited
that answer. Prices have a different lifecycle: US equities/ETFs trade in
premarket and after-hours windows, CME futures trade overnight, and crypto is
continuous. This module answers the price questions only. It never enables
option-chain work; callers pass its results to the price lane and to the
interface-facing session labels.

Sessions are derived from ET wall-clock rules plus the published exchange
calendar for US trading days. Early closes use an explicit table rather than a
guess. Unknown calendar data is reported as ``unknown`` with ``eligible=False``
for the affected group, never silently as an open market.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pytz import timezone as _tz

ET = _tz("America/New_York")

# Verified early closes for the 2026-2027 window covered by this release.
# NYSE closes at 13:00 ET; CME energy/metals close at 13:30 ET. Dates not in
# this table use the standard bounds and are labelled with their basis.
EQUITY_EARLY_CLOSES: dict[date, tuple[int, int]] = {
    date(2026, 11, 27): (13, 0),
    date(2026, 12, 24): (13, 0),
    date(2027, 11, 26): (13, 0),
}
FUTURES_EARLY_CLOSES: dict[date, tuple[int, int]] = {
    date(2026, 11, 27): (13, 30),
    date(2026, 12, 24): (13, 30),
    date(2027, 11, 26): (13, 30),
}
# Dates on which the CME energy/metals complex is fully closed.
FUTURES_FULL_CLOSURES: frozenset[date] = frozenset({
    date(2026, 1, 1), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 12, 25),
})

PREMARKET_OPEN = (4, 0)
REGULAR_OPEN = (9, 30)
REGULAR_CLOSE = (16, 0)
AFTERHOURS_CLOSE = (20, 0)
FUTURES_MAINTENANCE = ((17, 0), (18, 0))
FUTURES_SUNDAY_OPEN = (18, 0)
FX_SUNDAY_OPEN = (17, 0)

EQUITY_STATUSES = frozenset({"premarket", "regular", "afterhours"})
# Sessions in which a regular-session close is not a current price: the venue
# is trading, so the price basis must come from the active session's events.
EXTENDED_STATUSES = frozenset({"premarket", "afterhours"})
ACTIVE_STATUSES = frozenset({
    "premarket", "regular", "afterhours", "overnight", "continuous",
})

# Instrument families the lane can label without guessing. Everything else is
# reported as ``unknown`` while its daily source remains usable.
EQUITY_CLASSES = frozenset({"equity", "etf"})
FUTURES_CLASSES = frozenset({"commodity"})
CRYPTO_CLASSES = frozenset({"crypto"})


@dataclass(frozen=True)
class SessionState:
    """One instrument family's session at an instant, with its next change."""

    status: str
    eligible: bool
    next_transition_at: datetime | None
    basis: str
    reason: str | None = None
    early_close: bool = False

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class ExchangeCalendarUnavailable(RuntimeError):
    """Raised when US trading-day eligibility cannot be established safely."""


def _at(et: datetime, hour: int, minute: int = 0) -> datetime:
    return ET.localize(datetime(et.year, et.month, et.day, hour, minute))


def _next_day(et: datetime) -> datetime:
    return et + timedelta(days=1)


def _minutes(et: datetime) -> int:
    return et.hour * 60 + et.minute


def _first_after(et: datetime, candidates: list[datetime]) -> datetime | None:
    future = [candidate for candidate in candidates if candidate > et]
    return min(future) if future else None


class EquityCalendar:
    """A bounded set of US trading days, built once per lane day.

    The lane refreshes this at ET date rollover. Tests can construct it with a
    fixed set, so no network/calendar dependency enters the unit suite.
    """

    def __init__(self, trading_days: set[date], basis: str = "nyse_calendar_valid_days"):
        self.trading_days = frozenset(trading_days)
        self.basis = basis

    def is_trading_day(self, day: date) -> bool:
        return day in self.trading_days


def build_equity_calendar(et: datetime, exchange_dates=None) -> EquityCalendar:
    """Build the calendar from the collector's single exchange-date source.

    ``exchange_dates`` defaults to the package entry's patchable function, so
    the existing auth-safety test surface stays the only calendar authority.
    """
    if exchange_dates is None:
        import market_data.live_market as _entry
        exchange_dates = _entry._exchange_session_dates
    start = et.date() - timedelta(days=7)
    end = et.date() + timedelta(days=21)
    try:
        days = exchange_dates(start, end)
    except Exception as exc:
        raise ExchangeCalendarUnavailable(type(exc).__name__) from exc
    return EquityCalendar(set(days))


def equity_session(et: datetime, calendar: EquityCalendar | None) -> SessionState:
    """US equity/ETF session: 04:00-09:30, 09:30-16:00, 16:00-20:00 ET."""
    if calendar is None:
        return SessionState("unknown", False, None, "exchange_calendar_unavailable",
                            "calendar_not_built")
    day = et.date()
    if not calendar.is_trading_day(day):
        # Weekends and exchange holidays are both non-trading days; the label
        # distinguishes them only when the weekday is unambiguous.
        status = "closed" if day.weekday() >= 5 else "holiday"
        return SessionState(status, False, _next_equity_open(et, calendar), calendar.basis)
    early = EQUITY_EARLY_CLOSES.get(day)
    close_hour, close_minute = early or REGULAR_CLOSE
    minutes = _minutes(et)
    pre_open = _minutes(_at(et, *PREMARKET_OPEN))
    regular_open = _minutes(_at(et, *REGULAR_OPEN))
    regular_close = _minutes(_at(et, close_hour, close_minute))
    after_close = _minutes(_at(et, *AFTERHOURS_CLOSE))
    if minutes < pre_open:
        status, nxt = "closed", _at(et, *PREMARKET_OPEN)
    elif minutes < regular_open:
        status, nxt = "premarket", _at(et, *REGULAR_OPEN)
    elif minutes < regular_close:
        status, nxt = "regular", _at(et, close_hour, close_minute)
    elif minutes < after_close:
        status, nxt = "afterhours", _at(et, *AFTERHOURS_CLOSE)
    else:
        status, nxt = "closed", _next_equity_open(et, calendar)
    return SessionState(status, status in EQUITY_STATUSES, nxt, calendar.basis,
                        early_close=bool(early))


def equity_session_start(et: datetime, calendar: EquityCalendar | None) -> datetime | None:
    """Start instant (ET) of the active equity/ETF session, else ``None``.

    Price selection needs the instant the current session began: a trade
    printed before it belongs to an earlier session and cannot be presented as
    the current extended-hours price. Early-close days start after-hours at the
    verified early close, not at 16:00.
    """
    state = equity_session(et, calendar)
    if state.status == "premarket":
        return _at(et, *PREMARKET_OPEN)
    if state.status == "regular":
        return _at(et, *REGULAR_OPEN)
    if state.status == "afterhours":
        return _at(et, *(EQUITY_EARLY_CLOSES.get(et.date()) or REGULAR_CLOSE))
    return None


def _next_equity_open(et: datetime, calendar: EquityCalendar) -> datetime | None:
    """Next 04:00 ET on a trading day, including today when still ahead."""
    candidate_day = et.date()
    if calendar.is_trading_day(candidate_day):
        opening = _at(et, *PREMARKET_OPEN)
        if opening > et:
            return opening
    for offset in range(1, 22):
        day = et.date() + timedelta(days=offset)
        if calendar.is_trading_day(day):
            return ET.localize(datetime(day.year, day.month, day.day, *PREMARKET_OPEN))
    return None


def futures_session(et: datetime) -> SessionState:
    """CME Globex energy/metals: 18:00 Sunday through 17:00 Friday, 17-18 break.

    The contract's own expiry/roll is a row-level concern (``resolved_contract``);
    this function only labels the venue clock.
    """
    day = et.date()
    minutes = _minutes(et)
    maintenance_start = _minutes(_at(et, *FUTURES_MAINTENANCE[0]))
    maintenance_end = _minutes(_at(et, *FUTURES_MAINTENANCE[1]))
    if day in FUTURES_FULL_CLOSURES:
        return SessionState("holiday", False, _next_futures_open(et), "cme_energy_metals_holiday_table")
    early = FUTURES_EARLY_CLOSES.get(day)
    weekday = day.weekday()
    if weekday == 5:  # Saturday
        return SessionState("closed", False, _next_futures_open(et), "cme_globex_standard_rules")
    if weekday == 6:  # Sunday
        if minutes < _minutes(_at(et, *FUTURES_SUNDAY_OPEN)):
            return SessionState("closed", False, _at(et, *FUTURES_SUNDAY_OPEN), "cme_globex_standard_rules")
        return SessionState("overnight", True, _next_futures_transition(et, None),
                            "cme_globex_standard_rules")
    if weekday == 4 and minutes >= maintenance_start:  # Friday 17:00 close
        return SessionState("closed", False, _next_futures_open(et), "cme_globex_standard_rules")
    if early and minutes >= _minutes(_at(et, *early)):
        return SessionState("closed", False, _next_futures_open(et),
                            "cme_energy_metals_early_close_table", early_close=True)
    if maintenance_start <= minutes < maintenance_end:
        return SessionState("maintenance", False, _at(et, *FUTURES_MAINTENANCE[1]),
                            "cme_globex_standard_rules")
    status = "regular" if _minutes(_at(et, *REGULAR_OPEN)) <= minutes < _minutes(_at(et, *REGULAR_CLOSE)) else "overnight"
    return SessionState(status, True, _next_futures_transition(et, early),
                        "cme_globex_standard_rules", early_close=bool(early))


def _next_futures_transition(et: datetime, early) -> datetime | None:
    minutes = _minutes(et)
    candidates: list[datetime] = []
    if early:
        candidates.append(_at(et, *early))
    candidates.extend([_at(et, *FUTURES_MAINTENANCE[0]), _at(et, *FUTURES_MAINTENANCE[1])])
    candidates.append(_at(_next_day(et), *FUTURES_MAINTENANCE[0]))
    return _first_after(et, candidates)


def _next_futures_open(et: datetime) -> datetime | None:
    """Next 18:00 ET Sunday opening after a weekend/holiday close."""
    for offset in range(0, 10):
        day = et.date() + timedelta(days=offset)
        if day.weekday() == 6:
            opening = ET.localize(datetime(day.year, day.month, day.day, *FUTURES_SUNDAY_OPEN))
            if opening > et:
                return opening
    return None


def crypto_session(et: datetime) -> SessionState:
    """Crypto is continuous; provider availability is a freshness concern."""
    return SessionState("continuous", True, None, "crypto_24_7")


def fx_session(et: datetime) -> SessionState:
    """Spot FX: Sunday 17:00 through Friday 17:00 ET."""
    weekday = et.date().weekday()
    minutes = _minutes(et)
    if weekday == 5:
        return SessionState("closed", False, _next_fx_open(et), "spot_fx_weekday_rules")
    if weekday == 6:
        if minutes < _minutes(_at(et, *FX_SUNDAY_OPEN)):
            return SessionState("closed", False, _at(et, *FX_SUNDAY_OPEN), "spot_fx_weekday_rules")
        return SessionState("overnight", True, _at(_next_day(et), *FUTURES_MAINTENANCE[0]), "spot_fx_weekday_rules")
    if weekday == 4 and minutes >= _minutes(_at(et, *FUTURES_MAINTENANCE[0])):
        return SessionState("closed", False, _next_fx_open(et), "spot_fx_weekday_rules")
    return SessionState("overnight", True, _at(_next_day(et), *FUTURES_MAINTENANCE[0]), "spot_fx_weekday_rules")


def _next_fx_open(et: datetime) -> datetime | None:
    for offset in range(0, 10):
        day = et.date() + timedelta(days=offset)
        if day.weekday() == 6:
            opening = ET.localize(datetime(day.year, day.month, day.day, *FX_SUNDAY_OPEN))
            if opening > et:
                return opening
    return None


def daily_session(et: datetime) -> SessionState:
    """Official daily series: a fresh retrieval is a dated reading, not a tick."""
    return SessionState("daily", False, None, "daily_series")


def session_for(instrument: dict, et: datetime,
                calendar: EquityCalendar | None) -> SessionState:
    """Label one registry instrument with its own family's session rules."""
    asset_class = instrument.get("asset_class")
    profile = instrument.get("profile")
    if asset_class in CRYPTO_CLASSES or profile == "C":
        return crypto_session(et)
    if profile == "FU" or asset_class in FUTURES_CLASSES:
        return futures_session(et)
    if asset_class in EQUITY_CLASSES or profile in {"E", "V"}:
        # VIX/OVX are Cboe index products with an extended equity-like window.
        return equity_session(et, calendar)
    if asset_class == "fx" or profile == "F":
        return fx_session(et)
    return daily_session(et)


def session_start_for(instrument: dict, et: datetime,
                      calendar: EquityCalendar | None) -> datetime | None:
    """Start of the active session where a prior-session trade is not current.

    Only US equities/ETFs carry the extended-hours basis rule; futures, crypto,
    FX and daily series keep their existing selection because their provider
    events are provider-timed or explicitly dated.
    """
    asset_class = instrument.get("asset_class")
    profile = instrument.get("profile")
    if asset_class in CRYPTO_CLASSES or profile == "C":
        return None
    if profile == "FU" or asset_class in FUTURES_CLASSES:
        return None
    if asset_class in EQUITY_CLASSES or profile in {"E", "V"}:
        return equity_session_start(et, calendar)
    return None


def lane_summary(et: datetime, calendar: EquityCalendar | None) -> dict:
    """The price lane's current mode and next clock change.

    ``eligible`` is true while any provider-backed group is open. Crypto keeps
    it true at weekends by design; that is the requested coverage, not an
    accident of the clock.
    """
    equity = equity_session(et, calendar)
    futures = futures_session(et)
    crypto = crypto_session(et)
    fx = fx_session(et)
    groups = {"equities": equity.active, "futures": futures.active,
              "crypto": crypto.active, "fx": fx.active}
    if equity.active:
        mode = "equities"
    elif futures.active:
        mode = "futures"
    elif crypto.active:
        mode = "crypto"
    elif fx.active:
        mode = "fx"
    else:
        mode = "none"
    transitions = [state.next_transition_at for state in (equity, futures, crypto, fx)
                   if state.next_transition_at is not None]
    return {
        "mode": mode,
        "groups_open": groups,
        "eligible": any(groups.values()),
        "sessions": {"equities": equity, "futures": futures, "crypto": crypto, "fx": fx},
        "next_transition_at": min(transitions) if transitions else None,
        "basis": "explicit_family_sessions",
    }


def lane_summary_payload(et: datetime, calendar: EquityCalendar | None) -> dict:
    """JSON-safe shape for the published artifact and health record."""
    summary = lane_summary(et, calendar)
    return {
        "mode": summary["mode"],
        "groups_open": summary["groups_open"],
        "eligible": summary["eligible"],
        "next_transition_at": (summary["next_transition_at"].isoformat()
                               if summary["next_transition_at"] else None),
        "basis": summary["basis"],
        "sessions": {
            name: {
                "status": state.status,
                "eligible": state.eligible,
                "next_transition_at": (state.next_transition_at.isoformat()
                                       if state.next_transition_at else None),
                "basis": state.basis,
                "reason": state.reason,
                "early_close": state.early_close,
            }
            for name, state in summary["sessions"].items()
        },
    }


__all__ = [
    "ET", "SessionState", "ExchangeCalendarUnavailable", "EquityCalendar",
    "EQUITY_EARLY_CLOSES", "FUTURES_EARLY_CLOSES", "FUTURES_FULL_CLOSURES",
    "EXTENDED_STATUSES", "equity_session_start", "session_start_for",
    "build_equity_calendar", "equity_session", "futures_session", "crypto_session",
    "fx_session", "daily_session", "session_for", "lane_summary",
    "lane_summary_payload",
]
