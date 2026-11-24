"""Candle aggregation for the NASDAQ-100 collector.

Yahoo Finance serves real 1-minute bars; the tracker exposes 1m/3m/5m/9m/
15m/30m/1h/2h/4h/1d/1w intervals per stock. The non-standard intervals
(3m, 9m, 2h, 4h) are not native Yahoo intervals, so they are aggregated here
from the real 1-minute bars on America/New_York time, bucketed from the
09:30 ET regular-session open. Only regular-session bars (09:30-16:00 ET)
are aggregated; pre/post-market bars are dropped, never silently folded in.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)

# Minutes per interval. 1d/1w are native Yahoo intervals, not aggregations.
INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "9m": 9, "15m": 15, "30m": 30,
                    "1h": 60, "2h": 120, "4h": 240}
SUPPORTED_INTERVALS = tuple(INTERVAL_MINUTES) + ("1d", "1w")

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def _et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET)


def _session_open_et(day: datetime) -> datetime:
    et_day = _et(day)
    return et_day.replace(hour=9, minute=30, second=0, microsecond=0)


def aggregate_bars(bars, interval):
    """Aggregate 1-minute bars into ``interval`` bars.

    ``bars`` is a list of dicts with ``t`` (epoch seconds or an aware
    datetime), ``open``, ``high``, ``low``, ``close`` and ``volume``.
    ``interval`` is one of ``3m``/``5m``/``9m``/``15m``/``30m``/``1h``/``2h``/
    ``4h`` (minutes) or ``1m`` (passthrough). Buckets align to the 09:30 ET
    session open of the bar's own ET date, so a 9m bucket always covers
    09:30-09:39, 09:39-09:48, ... — never a wall-clock boundary that straddles
    the open. Returns bars sorted by start, each with ``start``/``end`` as
    ISO-8601 ET timestamps plus OHLCV and ``bars`` (input-bar count).
    """
    if interval == "1m":
        out = []
        for bar in sorted(bars, key=lambda b: _bar_time(b)):
            start = _bar_time(b)
            out.append({"start": start.isoformat(), "end": (start + timedelta(minutes=1)).isoformat(),
                        "open": bar["open"], "high": bar["high"], "low": bar["low"],
                        "close": bar["close"], "volume": bar.get("volume"),
                        "bars": 1})
        return out
    if interval not in INTERVAL_MINUTES:
        raise ValueError(f"unsupported aggregation interval: {interval!r}")
    width = INTERVAL_MINUTES[interval]
    buckets = {}
    for bar in bars:
        start = _bar_time(bar)
        if not (SESSION_OPEN <= start.time() < SESSION_CLOSE):
            continue  # outside the regular session: dropped, not folded in
        open_et = _session_open_et(start)
        minutes_since_open = int((start - open_et).total_seconds() // 60)
        if minutes_since_open < 0:
            continue
        bucket_start = open_et + timedelta(minutes=(minutes_since_open // width) * width)
        key = bucket_start
        slot = buckets.get(key)
        if slot is None:
            slot = buckets[key] = {"start": bucket_start, "open": bar["open"],
                                   "high": bar["high"], "low": bar["low"],
                                   "close": bar["close"], "volume": 0, "bars": 0}
        slot["high"] = max(slot["high"], bar["high"])
        slot["low"] = min(slot["low"], bar["low"])
        slot["close"] = bar["close"]
        slot["volume"] = (slot["volume"] or 0) + (bar.get("volume") or 0)
        slot["bars"] += 1
    out = []
    for bucket_start in sorted(buckets):
        slot = buckets[bucket_start]
        out.append({"start": bucket_start.isoformat(),
                    "end": (bucket_start + timedelta(minutes=width)).isoformat(),
                    "open": slot["open"], "high": slot["high"], "low": slot["low"],
                    "close": slot["close"], "volume": slot["volume"],
                    "bars": slot["bars"]})
    return out


def _bar_time(bar):
    t = bar["t"]
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t, tz=UTC).astimezone(ET)
    if isinstance(t, datetime):
        return _et(t)
    raise TypeError("bar 't' must be epoch seconds or an aware datetime")


def parse_yahoo_chart(payload):
    """Parse a Yahoo Finance chart-API response into 1-minute bars.

    Returns ``{"bars": [...], "meta": {...}}``; a payload with no usable
    timestamps/closes yields an empty bar list, never invented prices.
    """
    result = (payload or {}).get("chart", {}).get("result") or []
    if not result:
        return {"bars": [], "meta": {}}
    first = result[0]
    meta = first.get("meta") or {}
    timestamps = first.get("timestamp") or []
    quote = (first.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    bars = []
    for i, ts in enumerate(timestamps):
        close = closes[i] if i < len(closes) else None
        if ts is None or close is None:
            continue
        bars.append({"t": ts,
                     "open": opens[i] if i < len(opens) else close,
                     "high": highs[i] if i < len(highs) else close,
                     "low": lows[i] if i < len(lows) else close,
                     "close": close,
                     "volume": volumes[i] if i < len(volumes) else None})
    return {"bars": bars, "meta": meta}


# Nasdaq requires browser-style headers; identify the collector explicitly
# when using Yahoo's chart endpoint rather than inheriting Nasdaq's headers.
YAHOO_HEADERS = {"User-Agent": "OptionsView/1.0", "Accept": "*/*"}


def fetch_1m_bars(symbol, session, range_="5d"):
    """Fetch real 1-minute bars for ``symbol`` from Yahoo Finance.

    ``session`` is a ``requests.Session`` (or compatible object with
    ``.get``). Returns the ``parse_yahoo_chart`` dict. Raises on transport
    or HTTP errors; the caller records the symbol as source-unavailable.
    """
    url = YAHOO_CHART_URL.format(symbol=symbol)
    resp = session.get(url, params={"interval": "1m", "range": range_,
                                    "includePrePost": "false"}, headers=YAHOO_HEADERS, timeout=20)
    resp.raise_for_status()
    return parse_yahoo_chart(resp.json())


def fetch_daily_bars(symbol, session, range_="10d"):
    """Fetch daily bars for ``symbol`` (used for the 7-day change)."""
    url = YAHOO_CHART_URL.format(symbol=symbol)
    resp = session.get(url, params={"interval": "1d", "range": range_}, headers=YAHOO_HEADERS, timeout=20)
    resp.raise_for_status()
    return parse_yahoo_chart(resp.json())


def change_1h_pct(bars, now=None):
    """Percent change of the last 1m close vs the close ~60 minutes earlier.

    Anchored to the last bar's own time, not wall-clock ``now``: after the
    close, ``now`` is hours past the last bar, and anchoring to ``now``
    would compare the last close with itself (0.0%). Returns None when
    fewer than two distinct hours of bars are available; never substitutes
    a shorter window.
    """
    if len(bars) < 2:
        return None
    last_t = _bar_time(bars[-1])
    if now is not None:
        now_et = _et(now)
        anchor = min(now_et, last_t)
    else:
        anchor = last_t
    hour_ago = anchor - timedelta(hours=1)
    earlier = [b for b in bars if _bar_time(b) <= hour_ago]
    if not earlier:
        return None
    first_close = earlier[-1]["close"]
    last_close = bars[-1]["close"]
    if not first_close:
        return None
    return (last_close - first_close) / first_close * 100.0


def change_7d_pct(daily_bars):
    """Percent change of the last daily close vs the close 7 calendar days earlier.

    Uses the close 5 trading sessions back as the 7-day reference when a
    7-calendar-day bar is unavailable; returns None when fewer than two
    daily closes exist. The reference is labelled by its date in the caller.
    """
    closes = [b["close"] for b in daily_bars if b.get("close") is not None]
    if len(closes) < 2:
        return None
    reference = closes[-6] if len(closes) >= 6 else closes[0]
    if not reference:
        return None
    return (closes[-1] - reference) / reference * 100.0
