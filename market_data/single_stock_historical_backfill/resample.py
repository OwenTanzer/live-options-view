"""Derive the non-base candle intervals from the three Tiingo bases.

Never fetch a derived interval: Tiingo is only ever asked for ``1m``,
``1h`` and ``1d`` (see ``tiingo.py``). Everything else is aggregated
here, mirroring the bucketing in ``market_data/nasdaq_collector/
candles.py`` (PR #391) so the two agree where they overlap:

- ``3m``/``5m``/``9m``/``15m``/``30m`` from the 1-minute base,
  bucketed from the 09:30 ET regular-session open of each bar's own
  ET date — a 9m bucket always covers 09:30-09:39, 09:39-09:48, ...,
  never a wall-clock boundary that straddles the open. Only
  regular-session bars (09:30-16:00 ET) are aggregated; bars outside
  the session are dropped, never folded in. (Tiingo 1-minute bars are
  already regular-session only — 390/day — so the filter is defence,
  not a transformation.)
- ``2h``/``4h`` from the hourly base. Tiingo hourly bars are
  wall-clock-hour buckets labelled by hour start in UTC (~6 per
  trading day, first 10:00 ET), so 2h bars pair consecutive hourly
  bars within the same ET session day (10:00-12:00, 12:00-14:00,
  14:00-16:00) and 4h bars group four (10:00-14:00, then the trailing
  14:00-16:00 pair). Groups never cross a session day, and a partial
  trailing group is kept — it is the live, still-forming bar.
- ``1w`` from the daily base: ISO calendar weeks (Monday-Sunday),
  open = first bar's open, close = last bar's close, high/low =
  extremes, volume = sum of daily volumes. A partial current week is
  kept.

Volume: Tiingo intraday bars carry no volume, so no derived
intraday bar carries a ``volume`` key either — not even null. Weekly
bars carry ``volume`` (summed from the daily base, which has it).
Every derived bar carries ``bars`` (the number of base bars folded
into it) and the file it is published in names its ``derived_from``
base; base intervals are published exactly as Tiingo returned them,
with no added fields.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)

INTERVALS = ("1m", "3m", "5m", "9m", "15m", "30m", "1h", "2h", "4h", "1d", "1w")
BASE_INTERVALS = ("1m", "1h", "1d")
# Derived interval -> the base it is aggregated from. Never fetched.
DERIVED_FROM = {"3m": "1m", "5m": "1m", "9m": "1m", "15m": "1m", "30m": "1m",
                "2h": "1h", "4h": "1h", "1w": "1d"}
INTRADAY_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "9m": 9, "15m": 15, "30m": 30,
                    "1h": 60, "2h": 120, "4h": 240}
HOURLY_GROUP_BARS = {"2h": 2, "4h": 4}


def _parse_time(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _et(moment):
    return moment.astimezone(ET)


def _format_time(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_open_et(moment):
    et_day = _et(moment)
    return et_day.replace(hour=9, minute=30, second=0, microsecond=0)


def aggregate_intraday(bars_1m, interval):
    """Aggregate 1-minute bars into a coarser intraday interval.

    ``bars_1m`` is a list of ``{"t", "open", "high", "low", "close"}``
    (no volume — Tiingo intraday bars carry none). ``interval`` is one
    of ``3m``/``5m``/``9m``/``15m``/``30m``/``1h``; ``1m`` returns the
    input unchanged. Buckets align to the 09:30 ET session open of the
    bar's own ET date. Returns bars sorted by start, each with ``t``
    (bucket start, ISO-8601 UTC), ``open``/``high``/``low``/``close``
    and ``bars`` (input-bar count). No ``volume`` key is ever added.
    """
    if interval == "1m":
        return [dict(bar) for bar in sorted(bars_1m, key=lambda b: b["t"])]
    if interval not in INTRADAY_MINUTES or interval == "1h":
        # 1h is a fetched base, not a derivation from 1m (60 x 1m bars
        # would disagree with Tiingo's own wall-clock-hour buckets).
        raise ValueError(f"unsupported 1-minute aggregation interval: {interval!r}")
    width = INTRADAY_MINUTES[interval]
    buckets = {}
    for bar in bars_1m:
        start = _parse_time(bar["t"])
        start_et = _et(start)
        if not (SESSION_OPEN <= start_et.time() < SESSION_CLOSE):
            continue  # outside the regular session: dropped, not folded in
        open_et = _session_open_et(start)
        minutes_since_open = int((start_et - open_et).total_seconds() // 60)
        if minutes_since_open < 0:
            continue
        bucket_start = open_et + timedelta(minutes=(minutes_since_open // width) * width)
        key = bucket_start
        slot = buckets.get(key)
        if slot is None:
            slot = buckets[key] = {"t": _format_time(bucket_start),
                                   "open": bar["open"], "high": bar["high"],
                                   "low": bar["low"], "close": bar["close"],
                                   "bars": 0}
        slot["high"] = max(slot["high"], bar["high"])
        slot["low"] = min(slot["low"], bar["low"])
        slot["close"] = bar["close"]
        slot["bars"] += 1
    return [buckets[key] for key in sorted(buckets)]


def aggregate_hourly(bars_1h, interval):
    """Aggregate hourly bars into ``2h``/``4h`` bars.

    Groups consecutive hourly bars within the same ET session day
    (2 bars per 2h group, 4 per 4h group); groups never cross a day
    boundary, and a partial trailing group is kept as the live,
    still-forming bar. Bars carry no ``volume`` key (the hourly base
    has none). Each returned bar has ``t`` (first bar's start),
    OHLC and ``bars`` (input-bar count).
    """
    if interval not in HOURLY_GROUP_BARS:
        raise ValueError(f"unsupported hourly aggregation interval: {interval!r}")
    width = HOURLY_GROUP_BARS[interval]
    ordered = sorted(bars_1h, key=lambda b: b["t"])
    out = []
    current_day = None
    group = []

    def flush():
        if not group:
            return
        first, last = group[0], group[-1]
        out.append({
            "t": first["t"],
            "open": first["open"],
            "high": max(b["high"] for b in group),
            "low": min(b["low"] for b in group),
            "close": last["close"],
            "bars": len(group),
        })

    for bar in ordered:
        day = _et(_parse_time(bar["t"])).date()
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
    extremes, ``volume`` = sum of daily volumes (daily bars carry
    volume; a week with no volume anywhere carries no ``volume`` key),
    ``bars`` = trading days in the week. A partial current week is
    kept. ``t`` is the first bar's date (midnight UTC).
    """
    ordered = sorted(daily_bars, key=lambda b: b["t"])
    out = []
    current_key = None
    group = []

    def flush():
        if not group:
            return
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
        out.append(bar)

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
    returned exactly as fetched; derived intervals are aggregated here.
    """
    for base in BASE_INTERVALS:
        if base not in bases:
            raise ValueError(f"missing base dataset: {base!r}")
    derived = {
        "1m": [dict(bar) for bar in sorted(bases["1m"], key=lambda b: b["t"])],
        "1h": [dict(bar) for bar in sorted(bases["1h"], key=lambda b: b["t"])],
        "1d": [dict(bar) for bar in sorted(bases["1d"], key=lambda b: b["t"])],
    }
    for interval in ("3m", "5m", "9m", "15m", "30m"):
        derived[interval] = aggregate_intraday(bases["1m"], interval)
    for interval in ("2h", "4h"):
        derived[interval] = aggregate_hourly(bases["1h"], interval)
    derived["1w"] = aggregate_weekly(bases["1d"])
    return {interval: derived[interval] for interval in INTERVALS}
