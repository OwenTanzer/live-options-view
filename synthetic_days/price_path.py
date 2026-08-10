"""Block-bootstrap synthetic price paths from real QQQ 5-min bars.

Design choice: **resample real return sequences instead of fitting a
parametric model** (GARCH, jump-diffusion, ...). A moving block bootstrap
over real 5-min log-returns inherits the real stylized facts -- fat tails,
volatility clustering, the open/close volatility smile -- for free, because
the blocks *are* real observed sequences. A parametric model has to be
told about each of those effects and calibrated correctly; a bootstrap just
reuses ones that already happened. `validate_stats.py` checks this claim
rather than assuming it.

Two resampling modes:
  * `whole_day`  -- replay one real historical day's full return sequence
                    verbatim. Exactly reproduces one real day's
                    autocorrelation/vol-clustering structure, at the cost of
                    zero novelty (every synthetic day is a real day).
  * `block`      -- the default. Chop every historical day into fixed-size
                    blocks (default 6 bars = 30 min) and concatenate randomly
                    drawn blocks (from possibly different historical days,
                    always at matching intraday position) into a novel day.
                    Preserves local/short-range dependence within a block
                    while producing combinations that never literally
                    happened.

Within a 5-min bar, the collector actually needs a price roughly every 60s
(its snapshot cadence). Straight-line interpolation between 5-min closes
would erase all of that intrabar texture, so `upsample_to_seconds` instead
draws a Brownian bridge between consecutive bars, with the bridge's
volatility scaled to *that specific historical bar's own realized range*
(high-low) -- a quiet bar gets a quiet sub-path, a violent bar gets a noisy
one, same texture-matching principle as the block bootstrap itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")
BARS_PER_SESSION = 78  # 6.5h / 5min, 9:30-16:00 ET regular session


@dataclass
class DayBars:
    day: date
    t: np.ndarray  # unix seconds, len N
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray


def _to_et_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET).date()


def split_into_sessions(bars_by_year: dict[int, dict[str, list]]) -> list[DayBars]:
    """Flatten the columnar year files and group into regular-session days.

    Days with fewer than `BARS_PER_SESSION` bars (early closes, partial feed
    outages, the always-partial first/last day of the requested range) are
    dropped -- a bootstrap block drawn from a short day would silently shift
    every later block's time-of-day alignment for the rest of the synthetic
    day.
    """
    by_day: dict[date, list[tuple]] = {}
    for cols in bars_by_year.values():
        t, o, h, l, c, v = cols["t"], cols["o"], cols["h"], cols["l"], cols["c"], cols["v"]
        for i in range(len(t)):
            d = _to_et_date(int(t[i]))
            by_day.setdefault(d, []).append((t[i], o[i], h[i], l[i], c[i], v[i]))

    sessions = []
    for d, rows in sorted(by_day.items()):
        rows.sort(key=lambda r: r[0])
        if len(rows) < BARS_PER_SESSION:
            continue
        rows = rows[-BARS_PER_SESSION:]  # keep the latest N (drops any pre-open prints)
        arr = np.array(rows, dtype=float)
        sessions.append(DayBars(day=d, t=arr[:, 0], o=arr[:, 1], h=arr[:, 2], l=arr[:, 3], c=arr[:, 4], v=arr[:, 5]))
    return sessions


def _log_returns(bars: DayBars) -> np.ndarray:
    """Close-to-close log returns within a day, first bar's return is open->close."""
    closes = np.concatenate([[bars.o[0]], bars.c])
    return np.diff(np.log(closes))


def _intrabar_range_pct(bars: DayBars) -> np.ndarray:
    """(high-low)/close per bar -- the realized-range signal the upsampler scales its noise to."""
    return (bars.h - bars.l) / np.where(bars.c != 0, bars.c, np.nan)


def bootstrap_day(
    sessions: list[DayBars],
    rng: np.random.Generator,
    mode: str = "block",
    block_size: int = 6,
    start_price: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Returns (closes, intrabar_range_pct), each length BARS_PER_SESSION.

    `start_price` anchors the synthetic day's open; defaults to a randomly
    drawn historical day's own open so the *level* is realistic too (QQQ at
    50 behaves differently than QQQ at 550), not just the shape of returns.
    """
    n = BARS_PER_SESSION
    all_returns = [_log_returns(s) for s in sessions]
    all_ranges = [_intrabar_range_pct(s) for s in sessions]

    if start_price is None:
        start_price = float(rng.choice([s.o[0] for s in sessions]))

    if mode == "whole_day":
        idx = rng.integers(0, len(sessions))
        rets = all_returns[idx]
        ranges = all_ranges[idx]
    elif mode == "block":
        rets = np.empty(n)
        ranges = np.empty(n)
        pos = 0
        while pos < n:
            take = min(block_size, n - pos)
            day_idx = rng.integers(0, len(sessions))
            # Same intraday position in the donor day, so a block drawn for
            # "10:00-10:30" always comes from some real day's 10:00-10:30 --
            # this is what preserves the open/close volatility smile instead
            # of scrambling it.
            start = pos
            rets[pos:pos + take] = all_returns[day_idx][start:start + take]
            ranges[pos:pos + take] = all_ranges[day_idx][start:start + take]
            pos += take
    else:
        raise ValueError(f"unknown mode {mode!r}")

    closes = start_price * np.exp(np.cumsum(rets))
    return closes, ranges


def upsample_to_seconds(
    session_date: date,
    closes: np.ndarray,
    intrabar_range_pct: np.ndarray,
    rng: np.random.Generator,
    step_s: int = 60,
    bar_s: int = 300,
) -> tuple[list[datetime], np.ndarray]:
    """5-min closes -> a `step_s`-cadence path via per-bar Brownian bridges.

    Each bridge's endpoints are pinned to the real (bootstrapped) 5-min
    closes; its diffusion volatility inside the bar is set from that bar's
    own `intrabar_range_pct`, so a bar that was actually calm produces a
    calm sub-path and a bar that was actually violent produces a noisy one,
    rather than every bar getting the same synthetic intrabar noise level.
    """
    session_open = datetime.combine(session_date, datetime.min.time(), tzinfo=ET).replace(hour=9, minute=30)
    opens = np.concatenate([[closes[0] / np.exp(0)], closes[:-1]])  # bar i opens at bar i-1's close
    opens[0] = closes[0] * np.exp(-0.0)  # placeholder overwritten by caller with true day-open if desired

    timestamps: list[datetime] = []
    path: list[float] = []
    steps_per_bar = max(1, bar_s // step_s)

    prev_close = None
    for i in range(len(closes)):
        bar_start = session_open + timedelta(seconds=i * bar_s)
        bar_open = prev_close if prev_close is not None else closes[0] * np.exp(-_safe(intrabar_range_pct[i]) / 2)
        bar_close = closes[i]
        sigma = _safe(intrabar_range_pct[i]) * bar_open  # absolute-price vol budget for this bar

        # Brownian bridge: n sub-steps, endpoints fixed, interior steps drawn
        # then rescaled so the bridge actually lands on bar_close.
        n_steps = steps_per_bar
        raw = rng.normal(0.0, sigma / max(n_steps, 1) ** 0.5, size=n_steps)
        cum = np.cumsum(raw)
        drift = np.linspace(0, 1, n_steps + 1)[1:]
        bridge = bar_open + cum - drift * (cum[-1] - (bar_close - bar_open))

        for j in range(n_steps):
            timestamps.append(bar_start + timedelta(seconds=(j + 1) * step_s))
            path.append(float(bridge[j]))

        prev_close = bar_close

    return timestamps, np.array(path)


def _safe(x: float) -> float:
    return float(x) if x == x and x not in (float("inf"), float("-inf")) else 0.003  # ~30bp fallback range
