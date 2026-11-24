"""Derive the non-base candle intervals from the three crypto bases.

Never fetch a derived interval: the sources are only ever asked
for ``1m``, ``1h`` and ``1d`` (see :mod:`coinbase`, :mod:`tiingo`,
:mod:`coingecko`). Everything else is aggregated here.

The one deliberate difference from the stock backfill's resampler
(``market_data/single_stock_historical_backfill/resample.py``, PR
#393) is the session: **crypto trades 24/7, so there is no
09:30 ET session open and no out-of-session bar to drop**. Buckets
align to **00:00 UTC** instead:

- ``3m``/``5m``/``9m``/``15m``/``30m`` from the 1-minute base,
  bucketed from 00:00 UTC of each bar's own UTC date — a 9m bucket
  always covers 00:00–00:09, 00:09–00:18, ..., never a wall-clock
  boundary that straddles midnight. A partial trailing bucket is
  kept: it is the live, still-forming bar.
- ``2h``/``4h`` from the hourly base. Coinbase hourly bars are
  wall-clock-hour buckets labelled by hour start in UTC, so 2h bars
  pair consecutive hourly bars within the same UTC day
  (00:00–02:00, 02:00–04:00, ...) and 4h bars group four. Groups
  never cross a UTC day, and a partial trailing group is kept.
- ``1w`` from the daily base: ISO calendar weeks (Monday–Sunday),
  open = first bar's open, close = last bar's close, high/low =
  extremes, volume = sum of daily volumes where present. A partial
  current week is kept.

Volume: Coinbase bars carry volume on every interval, so a crypto
derived bar's volume is the sum of its base bars' volumes. A base
without volume (CoinGecko daily) yields derived bars with no
``volume`` key — not even null. Every derived bar carries ``bars``
(the number of base bars folded into it) and ``origin`` is
``"reconstructed"``; base bars are ``"recorded"`` and are published
exactly as fetched, with no added fields beyond the One Clock
envelope (``observed_at``, ``origin``) attached by the writer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc

INTERVALS = ("1m", "3m", "5m", "9m", "15m", "30m", "1h", "2h", "4h", "1d", "1w")
BASE_INTERVALS = ("1m", "1h", "1d")
# Derived interval -> the base it is aggregated from. Never fetched.
DERIVED_FROM = {"3m": "1m", "5m": "1m", "9m": "1m", "15m": "1m", "30m": "1m",
                "2h": "1h", "4h": "1h", "1w": "1d"}
INTRADAY_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "9m": 9, "15m": 15, "30m": 30,
                    "1h": 60, "2h": 120, "4h": 240}
HOURLY_GROUP_BARS = {"2h": 2, "4h": 4}
# Bar width in minutes, for the One Clock event time (bar close) and
# the reader's gap detection.
INTERVAL_MINUTES = dict(INTRADAY_MINUTES)
INTERVAL_MINUTES.update({"1d": 1440, "1w": 10080})


def _parse_time(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_time(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_midnight(moment):
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _fold(group):
    """Fold a non-empty group of base bars into one derived bar."""
    first, last = group[0], group[-1]
    bar = {
        "t": first["t"],
        "open": first["open"],
        "high": max(b["high"] for b in group),
        "low": min(b["low"] for b in group),
        "close": last["close"],
        "bars": len(group),
    }
    volumes = [b["volume"] for b in group if b.get("volume") is not None]
    if volumes:
        bar["volume"] = sum(volumes)
    return bar


def aggregate_intraday(bars_1m, interval):
    """Aggregate 1-minute bars into a coarser intraday interval.

    ``interval`` is one of ``3m``/``5m``/``9m``/``15m``/``30m``/``1h``;
    ``1m`` returns the input unchanged. Buckets align to 00:00 UTC
    of the bar's own UTC date (continuous 24/7 session, no session
    filter). Returns bars sorted by start, each with ``t`` (bucket
    start, ISO-8601 UTC), OHLC, ``volume`` (summed, when the base
    has it) and ``bars`` (input-bar count).
    """
    if interval == "1m":
        return [dict(bar) for bar in sorted(bars_1m, key=lambda b: b["t"])]
    if interval not in INTRADAY_MINUTES or interval == "1h":
        # 1h is a fetched base, not a derivation from 1m (60 x 1m bars
        # would disagree with the source's own wall-clock-hour buckets).
        raise ValueError(f"unsupported 1-minute aggregation interval: {interval!r}")
    width = INTRADAY_MINUTES[interval]
    buckets = {}
    for bar in bars_1m:
        start = _parse_time(bar["t"])
        midnight = _utc_midnight(start)
        minutes_since_midnight = int((start - midnight).total_seconds() // 60)
        bucket_start = midnight + timedelta(
            minutes=(minutes_since_midnight // width) * width)
        key = bucket_start
        slot = buckets.get(key)
        if slot is None:
            slot = buckets[key] = {"t": _format_time(bucket_start),
                                   "open": bar["open"], "high": bar["high"],
                                   "low": bar["low"], "close": bar["close"],
                                   "bars": 0}
            if bar.get("volume") is not None:
                slot["volume"] = 0.0
        slot["high"] = max(slot["high"], bar["high"])
        slot["low"] = min(slot["low"], bar["low"])
        slot["close"] = bar["close"]
        slot["bars"] += 1
        if bar.get("volume") is not None:
            slot["volume"] = slot.get("volume", 0.0) + bar["volume"]
    out = []
    for key in sorted(buckets):
        out.append(buckets[key])
    return out


def aggregate_hourly(bars_1h, interval):
    """Aggregate hourly bars into ``2h``/``4h`` bars.

    Groups consecutive hourly bars within the same UTC day (2 bars
    per 2h group, 4 per 4h group); groups never cross a UTC day, and
    a partial trailing group is kept as the live, still-forming bar.
    Each returned bar has ``t`` (first bar's start), OHLC, ``volume``
    (summed, when the base has it) and ``bars`` (input-bar count).
    """
    if interval not in HOURLY_GROUP_BARS:
        raise ValueError(f"unsupported hourly aggregation interval: {interval!r}")
    width = HOURLY_GROUP_BARS[interval]
    ordered = sorted(bars_1h, key=lambda b: b["t"])
    out = []
    current_day = None
    group = []

    def flush():
        if group:
            out.append(_fold(group))

    for bar in ordered:
        day = _parse_time(bar["t"]).date()
        if day != current_day or len(group) >= width:
            flush()
            group = []
            current_day = day
        group.append(bar)
    flush()
    return out


def aggregate_weekly(daily_bars):
    """Aggregate daily bars into weekly (Monday-Sunday) bars.

    Open = first bar's open, close = last bar's close, high/low =
    extremes, ``volume`` = sum of daily volumes (a week with no
    volume anywhere carries no ``volume`` key), ``bars`` = days in
    the week. A partial current week is kept. ``t`` is the first
    bar's date (midnight UTC).
    """
    ordered = sorted(daily_bars, key=lambda b: b["t"])
    out = []
    current_key = None
    group = []

    def flush():
        if group:
            out.append(_fold(group))

    for bar in ordered:
        day = _parse_time(bar["t"]).date()
        # ISO calendar week: Monday is the first day.
        key = (day - timedelta(days=day.weekday())).isoformat()
        if key != current_key:
            flush()
            group = []
            current_key = key
        group.append(bar)
    flush()
    return out


def derive_all(bases):
    """Derive all eleven intervals from the three fetched bases.

    ``bases`` is ``{"1m": [...], "1h": [...], "1d": [...]}``. Returns
    ``{interval: bars}`` for all of ``INTERVALS``. Base intervals are
    returned exactly as fetched; derived intervals are aggregated
    here. A base that is absent (CoinGecko coins have no 1m/1h base)
    yields no derived intervals from it — the caller records those
    as absent coverage, it never synthesises them.
    """
    derived = {}
    for base in BASE_INTERVALS:
        if base in bases:
            derived[base] = [dict(bar) for bar in
                             sorted(bases[base], key=lambda b: b["t"])]
    if "1m" in derived:
        for interval in ("3m", "5m", "9m", "15m", "30m"):
            derived[interval] = aggregate_intraday(derived["1m"], interval)
    if "1h" in derived:
        for interval in ("2h", "4h"):
            derived[interval] = aggregate_hourly(derived["1h"], interval)
    if "1d" in derived:
        derived["1w"] = aggregate_weekly(derived["1d"])
    return {interval: derived[interval] for interval in INTERVALS
            if interval in derived}
