"""Per-long-option European sensitivities at the snapshot clock (zero rates and yield)."""

import math
from datetime import date, datetime

from .config import STALE_FEED_SECS, log

FIELDS = ("ModelGamma", "ModelVanna", "ModelVega", "ModelCharm")


def current_event(stamp, now):
    try:
        age = (now - datetime.fromisoformat(stamp)).total_seconds()
    except (ValueError, TypeError):
        return False
    return 0 <= age <= STALE_FEED_SECS


def years_to_reference_close(expiration, now):
    # This is the cash-session close assumption, not an exercise cutoff.
    from .weekly import _weekly_session_bounds
    try:
        bounds = _weekly_session_bounds(date.fromisoformat(expiration))
    except Exception as exc:
        log.warning("Option model expiry clock unavailable: %s", exc)
        return None
    if bounds is None:
        return None
    seconds = (bounds[1] - now).total_seconds()
    return seconds / (365 * 86400) if seconds >= 1 else None


def model_greeks(spot, strike, iv, years):
    """Return gamma / $1, vanna & vega / 1 IV point, local charm / elapsed day."""
    empty = dict.fromkeys(FIELDS)
    if any(type(v) not in (float, int) or not math.isfinite(v) or v <= 0
           for v in (spot, strike, iv, years)):
        return empty
    root_t = math.sqrt(years)
    width = iv * root_t
    if width < 1e-12 or not math.isfinite(width):
        return empty
    d1 = (math.log(spot) - math.log(strike)) / width + width / 2
    d2 = d1 - width
    density = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    values = (density / spot / width, -density * d2 / iv / 100,
              spot * density * root_t / 100, density * d2 / (2 * years * 365))
    if not all(math.isfinite(v) for v in values):
        return empty
    return {name: float(f"{value:.9g}") for name, value in zip(FIELDS, values)}
