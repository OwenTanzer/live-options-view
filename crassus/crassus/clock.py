"""Eastern-Time-aware clock.

The runner's schedule, the audit trail, and every market-hours decision are
expressed in ET. UTC is kept alongside it so records stay unambiguous.
"""

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_et() -> datetime:
    return now_utc().astimezone(ET)


def iso_utc(dt: datetime | None = None) -> str:
    return (dt.astimezone(timezone.utc) if dt else now_utc()).isoformat()


def iso_et(dt: datetime | None = None) -> str:
    return (dt.astimezone(ET) if dt else now_et()).isoformat()


def session_phase(dt: datetime | None = None) -> str:
    """Coarse market phase: weekend | premarket | open | afterhours.

    Regular hours only -- this does not know about exchange holidays or early
    closes, so a holiday reads as `open`. The server rejects stale quotes
    anyway, which is the real backstop; this is a cheap way to avoid burning
    rate-limit budget when nothing can possibly fill.
    """
    et = dt.astimezone(ET) if dt else now_et()
    if et.weekday() >= 5:
        return "weekend"
    if et.time() < MARKET_OPEN:
        return "premarket"
    if et.time() >= MARKET_CLOSE:
        return "afterhours"
    return "open"


def is_tradeable(dt: datetime | None = None) -> bool:
    return session_phase(dt) == "open"
