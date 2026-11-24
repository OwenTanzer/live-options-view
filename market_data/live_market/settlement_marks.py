"""Publish new, immutable per-underlying expiry marks; never rewrite archives.

Only a timed post-close observation on the expiry date qualifies. Early-close
sessions without such an observation remain pending, requiring a separately
verified mark rather than borrowing an intraday or different underlying price.

`observed_at` must be the PROVIDER's own event time for the price being
marked -- never the collector receipt time. A fresh receipt of a price
whose provider observation time is unknown, pre-close, stale, future, or
mixed across the two midpoint sides must stay pending; the publisher
rejects all of those (unknown/stale/future fail the window/freshness
checks, and a mixed midpoint is stamped with its older side, which is
pre-close). Callers pair the price with its provider time via
quotes._provider_observed_spy_price (Brian #184's
UnderlyingSelection.provider_observed_at model); a remembered/carried-
forward spot has no provider time and can never publish.

Taking the min (oldest) contributing leg proves the oldest leg isn't
stale, but hides a future newer leg: a valid bid at 21:00:30 plus a
FUTURE ask at 21:01:01 (now 21:00:40) would publish stamped 21:00:30.
So callers must validate EVERY contributing provider timestamp with
contributing_provider_times_valid before publishing; the publisher alone
only sees the one `observed_at` it is given.
"""
import json
import math
from datetime import datetime

from .config import ET, R2_BUCKET


def _is_valid_observation(observed_at, expiration, now):
    """One provider timestamp is a valid post-close observation: it parses,
    is tz-aware, falls on the expiry date in the 16:00-16:15 ET window,
    and is fresh (age 0-120s -- not in the future, not stale)."""
    try:
        observed = datetime.fromisoformat(observed_at)
        if observed.tzinfo is None:
            return False
        local = observed.astimezone(ET)
        age = (now - observed).total_seconds()
    except (TypeError, ValueError):
        return False
    return (local.date().isoformat() == expiration and local.hour == 16
            and local.minute <= 15 and 0 <= age <= 120)


def contributing_provider_times_valid(provider_times, expiration, now):
    """Every contributing provider timestamp must be a valid post-close
    observation (see _is_valid_observation). An empty list is invalid
    (unknown evidence); a single future or out-of-window leg vetoes the
    mark even when the oldest leg is valid."""
    if not provider_times:
        return False
    return all(_is_valid_observation(ts, expiration, now) for ts in provider_times)


def publish_settlement_mark(s3, symbol, expiration, spot, observed_at, now):
    if symbol not in {"SPY", "IBIT", "SLV", "DRAM"} or not isinstance(spot, (int, float)) or not math.isfinite(spot) or spot <= 0:
        return False
    if not _is_valid_observation(observed_at, expiration, now):
        return False
    payload = {"schema_version": 1, "symbol": symbol, "expiration": expiration,
               "date": expiration, "spot": spot, "observed_at": observed_at,
               "source": "dxlink", "price_basis": "first_observed_post_close",
               "published_at": now.isoformat()}
    try:
        s3.put_object(Bucket=R2_BUCKET, Key=f"settlement/{symbol.lower()}/{expiration}.json",
                      Body=json.dumps(payload).encode(), ContentType="application/json", IfNoneMatch="*")
    except Exception as exc:
        response = getattr(exc, "response", {})
        if response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (409, 412):
            return False
        raise
    return True
