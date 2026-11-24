"""Next-hour price estimator for crypto predictions.

A pure function from recent 1h bars: no live-service dependency, no
clock, no I/O. The producer (:mod:`producer`) feeds it the cached
1h bars and the result is published as a One Clock observation with
``origin: "synthetic"`` — an estimate, never a recorded price (see
:mod:`predictions` and ``docs/specs/CRYPTO_PREDICTIONS.md``).

Signals (all computed from the bars handed in, nothing else):

- **last-hour momentum** — the most recent completed hourly return.
- **24h move** — the return over the last 24 hourly bars (or fewer
  when fewer bars exist; the window actually used is reported).
- **7d move** — the return over the last 168 hourly bars (or fewer).
- **hourly volatility** — the sample standard deviation of the
  hourly returns in the window (24 returns when available).
- **last-hour range** — ``(high - low) / close`` of the last bar.
- **BTC correlation** — the Pearson correlation of this coin's
  hourly returns with BTC's over the aligned window. ``None`` for
  BTC itself (the correlation of a series with itself is 1 by
  definition and carries no signal) and when too few aligned
  pairs exist.

The estimate:

- ``drift = 0.5 * momentum_1h + 0.25 * (move_24h / hours_24h)
  + 0.25 * (move_7d / hours_7d)`` — the last hour dominates; the
  longer moves enter as per-hour rates so a 7d drift does not
  swamp one hour.
- ``drift`` is clamped to ``±3 * hourly_volatility`` (with a
  ``0.2%`` floor on the clamp so a near-zero-volatility coin can
  still move a little). ``estimated_price = last_close * (1 +
  drift)``.
- The range is one hourly-volatility sigma around the estimate:
  ``estimated_price ± hourly_volatility * last_close``, with a
  ``0.1%`` floor on the half-width so the range always strictly
  contains the estimate even for a pegged coin.
- ``direction`` is ``"up"`` / ``"down"`` / ``"flat"`` on the sign
  of the clamped drift, with a ``0.05%`` dead band for flat.
- ``confidence`` is ``completeness * max(0, 1 - vol / 0.04)``,
  clamped to ``[0.05, 0.95]``, where ``completeness`` is the
  fraction of the 24-return window actually present. A coin with
  few bars, or wild hourly volatility, scores low; nothing here
  claims the estimate is likely right — confidence measures how
  much data and how calm a history the estimate rests on, not a
  probability of the outcome.

Coins with fewer than 2 hourly bars cannot be estimated at all
(:func:`estimate` returns ``None``); CoinGecko-labelled coins have
no 1h base by design (see the candle cache contract), so they
produce no prediction — absent, never interpolated. Pegged
stablecoins estimate flat by construction (near-zero volatility
and near-zero moves); the spec records that their estimates carry
no information and reviewers may exclude them.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

# The estimator window: 24 hourly returns (25 closes), 24h and 7d
# moves measured over the bars actually present.
WINDOW_RETURNS = 24
HOURS_24H = 24
HOURS_7D = 168
# Dead band for "flat", clamp floor, volatility scale for confidence.
FLAT_DEAD_BAND = 0.0005
CLAMP_VOL_FLOOR = 0.002
CONFIDENCE_VOL_SCALE = 0.04
RANGE_HALF_FLOOR_PCT = 0.001
MIN_BARS = 2
MIN_CORR_PAIRS = 8


def _parse_time(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _returns(closes):
    """Hourly simple returns from a list of closes (oldest first)."""
    return [closes[i] / closes[i - 1] - 1.0
            for i in range(1, len(closes)) if closes[i - 1] != 0]


def _stdev(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _pearson(xs, ys):
    """Pearson correlation, or None when it is undefined."""
    if len(xs) < MIN_CORR_PAIRS or len(xs) != len(ys):
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs)
                    * sum((y - my) ** 2 for y in ys))
    if den == 0:
        return None
    return num / den


def _aligned_returns(bars, btc_bars):
    """Returns of ``bars`` aligned by bar start with BTC's returns.

    Returns ``(coin_returns, btc_returns)`` over the shared bar
    starts, or ``(None, None)`` when BTC bars are absent.
    """
    if not btc_bars:
        return None, None
    btc_by_t = {b["t"]: b for b in btc_bars}
    coin_closes, btc_closes = [], []
    for bar in bars:
        other = btc_by_t.get(bar["t"])
        if other is not None:
            coin_closes.append(bar["close"])
            btc_closes.append(other["close"])
    if len(coin_closes) < MIN_BARS:
        return None, None
    return _returns(coin_closes), _returns(btc_closes)


def estimate(bars, btc_bars=None, symbol="BTC"):
    """Estimate the next-hour close from completed 1h bars.

    ``bars`` are completed 1h bars sorted by ``t`` (oldest first);
    only bars the caller has already established as closed may be
    passed in — a still-forming bar must never enter the window.
    ``btc_bars`` are BTC's 1h bars for the correlation signal
    (ignored when ``symbol == "BTC"``). Returns ``None`` when
    fewer than 2 bars are available, else a dict with
    ``estimated_price``, ``direction``, ``pct_move``,
    ``range_low``, ``range_high``, ``confidence``,
    ``last_close``, ``data_window`` and ``basis``.
    """
    ordered = sorted(bars, key=lambda b: b["t"])
    if len(ordered) < MIN_BARS:
        return None
    closes = [b["close"] for b in ordered]
    last = ordered[-1]
    last_close = last["close"]

    window = ordered[-(WINDOW_RETURNS + 1):]
    window_closes = [b["close"] for b in window]
    returns = _returns(window_closes)
    completeness = min(1.0, len(returns) / WINDOW_RETURNS)

    momentum_1h = returns[-1] if returns else 0.0
    hours_24h = min(HOURS_24H, len(closes) - 1)
    move_24h = (closes[-1] / closes[-1 - hours_24h] - 1.0
                if closes[-1 - hours_24h] != 0 else 0.0)
    hours_7d = min(HOURS_7D, len(closes) - 1)
    move_7d = (closes[-1] / closes[-1 - hours_7d] - 1.0
               if closes[-1 - hours_7d] != 0 else 0.0)
    volatility = _stdev(returns)
    last_range = ((last["high"] - last["low"]) / last_close
                  if last_close != 0 else 0.0)

    basis = ["last-hour momentum", "24h move", "7d move",
             "hourly volatility", "last-hour range"]
    btc_correlation = None
    if symbol != "BTC" and btc_bars:
        coin_ret, btc_ret = _aligned_returns(window, btc_bars)
        if coin_ret is not None and btc_ret is not None:
            btc_correlation = _pearson(coin_ret, btc_ret)
            if btc_correlation is not None:
                basis.append("BTC correlation")

    drift = (0.5 * momentum_1h
             + 0.25 * (move_24h / hours_24h)
             + 0.25 * (move_7d / hours_7d))
    clamp = max(3.0 * volatility, CLAMP_VOL_FLOOR)
    drift = max(-clamp, min(clamp, drift))

    estimated_price = last_close * (1.0 + drift)
    half_width = max(volatility * last_close,
                     RANGE_HALF_FLOOR_PCT * last_close)
    if drift > FLAT_DEAD_BAND:
        direction = "up"
    elif drift < -FLAT_DEAD_BAND:
        direction = "down"
    else:
        direction = "flat"
    confidence = max(0.05, min(0.95,
                                completeness * max(0.0, 1.0 - volatility / CONFIDENCE_VOL_SCALE)))
    return {
        "estimated_price": estimated_price,
        "direction": direction,
        "pct_move": drift * 100.0,
        "range_low": estimated_price - half_width,
        "range_high": estimated_price + half_width,
        "confidence": confidence,
        "last_close": last_close,
        "data_window": {
            "bars_1h": len(ordered),
            "returns": len(returns),
            "first_bar": ordered[0]["t"],
            "last_bar": last["t"],
            "hours_24h": hours_24h,
            "hours_7d": hours_7d,
        },
        "basis": basis,
        "signals": {
            "momentum_1h_pct": momentum_1h * 100.0,
            "move_24h_pct": move_24h * 100.0,
            "move_7d_pct": move_7d * 100.0,
            "hourly_volatility_pct": volatility * 100.0,
            "last_hour_range_pct": last_range * 100.0,
            "btc_correlation": btc_correlation,
        },
    }
