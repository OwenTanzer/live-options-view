"""Read-only trend-pattern detection for the crypto snapshot.

Same detection list as the Coinbase Price Tracker space, applied to
the collector's own snapshot instead of the tracker's page state.
Pure functions: no network, no clock reads inside the detectors
(``now`` is passed in), no favourites, no alerts, no user state.

Each pattern is a dict::

    {
      "id": "sharp_24h_move:BTC",        # stable within a run
      "dedupe_key": "sharp_24h_move:BTC", # stable across runs
      "kind": "sharp_24h_move",
      "severity": "notable" | "extreme" | "info",
      "title": "...",
      "detail": "...",
      "coins": ["BTC"],
      "detected_at": "<ISO-8601 UTC>",
    }

Dedupe: a pattern whose ``dedupe_key`` was already detected within
``DEDUPE_WINDOW_SECONDS`` (90 minutes, the tracker's window) is
suppressed. A pattern that is still true after the window re-appears;
dedupe never rewrites a pattern's content.
"""

from __future__ import annotations

from datetime import datetime, timezone

DEDUPE_WINDOW_SECONDS = 90 * 60

SHARP_24H_PCT = 10.0
EXTREME_24H_PCT = 20.0
MOMENTUM_MIN_24H_PCT = 5.0
SUSTAINED_7D_PCT = 15.0
JUMP_SINCE_LAST_PCT = 5.0
HIGH_LOW_TEST_PCT = 2.0
WIDE_RANGE_PCT = 10.0
ELEVATED_VOLUME_RATIO = 0.5  # 24h volume / market cap, no baseline
BREADTH_PCT = 70.0
BTC_ALT_DIVERGENCE_PP = 3.0
FEAR_GREED_EXTREME_LOW = 20
FEAR_GREED_EXTREME_HIGH = 80
DOMINANCE_SHIFT_PP = 2.0


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pattern(kind, severity, title, detail, coins, now, dedupe_key=None):
    key = dedupe_key or f"{kind}:{','.join(coins)}" if coins else kind
    return {
        "id": key,
        "dedupe_key": key,
        "kind": kind,
        "severity": severity,
        "title": title,
        "detail": detail,
        "coins": list(coins),
        "detected_at": _iso(now),
    }


def _num(row, field):
    value = row.get(field)
    return value if isinstance(value, (int, float)) and value == value else None


def detect_patterns(coins, market, previous, *, now):
    """Detect trend patterns from one snapshot's coin rows.

    ``coins``: snapshot coin rows (price fields may be null).
    ``market``: snapshot ``market`` block (may carry nulls).
    ``previous``: the previous snapshot dict, or None on first run.
    ``now``: aware datetime used for ``detected_at``.
    """
    patterns = []
    usable = [c for c in coins if c.get("status") == "ok" and _num(c, "price") is not None]

    prev_by_symbol = {}
    if previous:
        for row in previous.get("coins", []):
            prev_by_symbol[row.get("symbol")] = row

    for row in usable:
        symbol = row["symbol"]
        change_24h = _num(row, "change_24h_pct")
        change_1h = _num(row, "change_1h_pct")
        change_7d = _num(row, "change_7d_pct")
        price = _num(row, "price")
        high = _num(row, "high_24h")
        low = _num(row, "low_24h")
        volume = _num(row, "volume_24h")
        market_cap = _num(row, "market_cap")

        # 1. Sharp 24h moves.
        if change_24h is not None and abs(change_24h) >= SHARP_24H_PCT:
            severity = "extreme" if abs(change_24h) >= EXTREME_24H_PCT else "notable"
            direction = "up" if change_24h > 0 else "down"
            patterns.append(_pattern(
                "sharp_24h_move", severity,
                f"{symbol} moved {change_24h:+.1f}% in 24h",
                f"{symbol} is {direction} {abs(change_24h):.1f}% over the last 24 hours.",
                [symbol], now))

        # 2/3. Momentum alignment / divergence (1h vs 24h, 7d confirms).
        if (change_1h is not None and change_24h is not None
                and abs(change_24h) >= MOMENTUM_MIN_24H_PCT):
            aligned = (change_1h > 0) == (change_24h > 0)
            if aligned:
                patterns.append(_pattern(
                    "momentum_alignment", "info",
                    f"{symbol}: 1h and 24h momentum aligned",
                    f"{symbol} 1h {change_1h:+.1f}% and 24h {change_24h:+.1f}% point the same way.",
                    [symbol], now))
            else:
                patterns.append(_pattern(
                    "momentum_divergence", "notable",
                    f"{symbol}: 1h momentum diverges from 24h",
                    f"{symbol} 1h {change_1h:+.1f}% against 24h {change_24h:+.1f}%.",
                    [symbol], now))

        # 4. 24h high/low tests.
        if price is not None and high is not None and low is not None and high > low:
            if price >= high * (1 - HIGH_LOW_TEST_PCT / 100):
                patterns.append(_pattern(
                    "high_24h_test", "notable",
                    f"{symbol} is testing its 24h high",
                    f"{symbol} at ${price:,.4g} is within {HIGH_LOW_TEST_PCT}% of its 24h high ${high:,.4g}.",
                    [symbol], now))
            elif price <= low * (1 + HIGH_LOW_TEST_PCT / 100):
                patterns.append(_pattern(
                    "low_24h_test", "notable",
                    f"{symbol} is testing its 24h low",
                    f"{symbol} at ${price:,.4g} is within {HIGH_LOW_TEST_PCT}% of its 24h low ${low:,.4g}.",
                    [symbol], now))

        # 5. Wide-range volatility.
        if price is not None and high is not None and low is not None and price > 0:
            if (high - low) / price * 100 >= WIDE_RANGE_PCT:
                patterns.append(_pattern(
                    "wide_range_volatility", "notable",
                    f"{symbol}: wide 24h range",
                    f"{symbol} 24h range ${low:,.4g}–${high:,.4g} spans "
                    f"{(high - low) / price * 100:.1f}% of its price.",
                    [symbol], now))

        # 6. Elevated volume (volume / market cap; no history baseline).
        if volume is not None and market_cap is not None and market_cap > 0:
            if volume / market_cap >= ELEVATED_VOLUME_RATIO:
                patterns.append(_pattern(
                    "elevated_volume", "info",
                    f"{symbol}: elevated 24h volume",
                    f"{symbol} 24h volume ${volume:,.0f} is "
                    f"{volume / market_cap * 100:.0f}% of its market cap.",
                    [symbol], now))

        # 7. Sustained 7-day trends.
        if (change_7d is not None and change_24h is not None
                and abs(change_7d) >= SUSTAINED_7D_PCT
                and (change_7d > 0) == (change_24h > 0)):
            direction = "up" if change_7d > 0 else "down"
            patterns.append(_pattern(
                "sustained_7d_trend", "notable",
                f"{symbol}: sustained 7-day {direction} trend",
                f"{symbol} is {change_7d:+.1f}% over 7 days and {change_24h:+.1f}% over 24h.",
                [symbol], now))

        # 8. Jumps since the last update (needs a previous snapshot).
        prev = prev_by_symbol.get(symbol)
        prev_price = _num(prev, "price") if prev else None
        if price is not None and prev_price is not None and prev_price > 0:
            jump = (price - prev_price) / prev_price * 100
            if abs(jump) >= JUMP_SINCE_LAST_PCT:
                direction = "up" if jump > 0 else "down"
                patterns.append(_pattern(
                    "jump_since_last_update", "notable",
                    f"{symbol} jumped {jump:+.1f}% since the last update",
                    f"{symbol} moved {direction} {abs(jump):.1f}% since the previous hourly snapshot.",
                    [symbol], now))

    # 9. Broad rally / selloff breadth.
    if usable:
        ups = [c for c in usable if (_num(c, "change_24h_pct") or 0) > 0]
        downs = [c for c in usable if (_num(c, "change_24h_pct") or 0) < 0]
        for group, label in ((ups, "rally"), (downs, "selloff")):
            if len(group) / len(usable) * 100 >= BREADTH_PCT:
                patterns.append(_pattern(
                    f"broad_{label}", "notable",
                    f"Broad {label}: {len(group)} of {len(usable)} coins "
                    f"{'up' if label == 'rally' else 'down'} over 24h",
                    f"{len(group)} of {len(usable)} available coins moved "
                    f"{'up' if label == 'rally' else 'down'} in the last 24 hours.",
                    [c["symbol"] for c in group], now,
                    dedupe_key=f"broad_{label}"))

    # 10. BTC-vs-alts divergence.
    btc = next((c for c in usable if c["symbol"] == "BTC"), None)
    alts = [c for c in usable if c["symbol"] != "BTC"
            and _num(c, "change_24h_pct") is not None]
    if btc is not None and alts and _num(btc, "change_24h_pct") is not None:
        changes = sorted(_num(c, "change_24h_pct") for c in alts)
        median_alt = changes[len(changes) // 2]
        btc_change = _num(btc, "change_24h_pct")
        if ((btc_change > 0) != (median_alt > 0)
                and abs(btc_change - median_alt) >= BTC_ALT_DIVERGENCE_PP):
            patterns.append(_pattern(
                "btc_vs_alts_divergence", "notable",
                "Bitcoin is diverging from altcoins",
                f"Bitcoin {btc_change:+.1f}% over 24h against a median altcoin "
                f"{median_alt:+.1f}%.",
                ["BTC"], now, dedupe_key="btc_vs_alts_divergence"))

    # 11. Fear & Greed extremes.
    fear_greed = (market or {}).get("fear_greed") or {}
    value = fear_greed.get("value")
    if isinstance(value, (int, float)):
        if value <= FEAR_GREED_EXTREME_LOW:
            patterns.append(_pattern(
                "fear_greed_extreme", "extreme",
                f"Fear & Greed at {value:.0f} — extreme fear",
                f"The Fear & Greed index reads {value:.0f} "
                f"({fear_greed.get('classification') or 'unclassified'}).",
                [], now, dedupe_key="fear_greed_extreme"))
        elif value >= FEAR_GREED_EXTREME_HIGH:
            patterns.append(_pattern(
                "fear_greed_extreme", "extreme",
                f"Fear & Greed at {value:.0f} — extreme greed",
                f"The Fear & Greed index reads {value:.0f} "
                f"({fear_greed.get('classification') or 'unclassified'}).",
                [], now, dedupe_key="fear_greed_extreme"))

    # 12. Market-cap / dominance shifts (needs a previous snapshot).
    if previous:
        prev_market = previous.get("market") or {}
        dominance = (market or {}).get("btc_dominance_pct")
        prev_dominance = prev_market.get("btc_dominance_pct")
        if (isinstance(dominance, (int, float))
                and isinstance(prev_dominance, (int, float))
                and abs(dominance - prev_dominance) >= DOMINANCE_SHIFT_PP):
            patterns.append(_pattern(
                "dominance_shift", "info",
                f"Bitcoin dominance moved {dominance - prev_dominance:+.1f} points",
                f"Bitcoin dominance is {dominance:.1f}%, against "
                f"{prev_dominance:.1f}% at the previous update.",
                ["BTC"], now, dedupe_key="dominance_shift"))

    return patterns


def dedupe_patterns(patterns, previous_patterns, *, now):
    """Drop patterns already detected within the 90-minute window.

    ``previous_patterns`` is the previous snapshot's ``patterns`` list
    (each with ``dedupe_key`` and ``detected_at``). A pattern with no
    usable ``detected_at`` on the previous side is kept: dedupe must
    never suppress on the basis of an undated record.
    """
    if not previous_patterns:
        return list(patterns)
    now_dt = now if isinstance(now, datetime) else datetime.fromisoformat(now)
    recent = set()
    for prev in previous_patterns:
        try:
            seen = datetime.fromisoformat(
                str(prev["detected_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if (now_dt - seen).total_seconds() <= DEDUPE_WINDOW_SECONDS:
            recent.add(prev.get("dedupe_key"))
    return [p for p in patterns if p.get("dedupe_key") not in recent]
