"""NASDAQ-100 snapshot collection from public sources only.

Prices come from Nasdaq's official quote API (``api.nasdaq.com``) with a
browser-style User-Agent header; no account or auth. Candles come from the
Yahoo Finance chart API and headlines from Google News RSS (see
``candles.py`` and ``news.py``).

Session model (stated explicitly): US equity sessions, weekdays 09:30-16:00
America/New_York. ``market_status`` is ``open`` only inside those hours on
a non-holiday weekday; otherwise ``closed`` with a reason (weekend,
holiday, outside hours). The embedded holiday calendar covers 2026 only
(NYSE holidays); for other years holidays are not asserted and the status
falls back to the weekday/hours rule, labelled as such. A stock whose
source is unavailable is reported with ``status: "source_unavailable"``
and null price fields — a price is never substituted, carried forward, or
invented.

The two percentage bases are labelled differently, as the tracker does:

- per-stock ``percent_change``: Nasdaq's latest reported move, i.e. the
  after-hours move after the close (``STOCK_PCT_BASIS``);
- index ``percent_change``: the regular-session move vs the previous close
  (``INDEX_PCT_BASIS``).

Nasdaq's quote endpoint publishes a per-quote last-trade timestamp
(``primaryData.lastTradeTimestamp``, e.g. "Nov 23, 2026 6:38 PM ET"), so
``provider_observed_at`` carries it when present and freshness is measured
from it; when only a date is published (as for NDX) the timestamp is null
and freshness falls back to collector receipt. That basis is stated on
every row rather than implied. ``secondaryData`` is the regular-session
close; ``primaryData`` is the latest (after-hours) quote, which is what
the per-stock price/percent report.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from . import candles as candles_mod
from . import news as news_mod
from .symbols import EXPECTED_LISTINGS, SOURCE_AS_OF, SOURCE_LABEL, SYMBOLS, names, validate

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
NASDAQ_QUOTE_URL = "https://api.nasdaq.com/api/quote/{symbol}/info"
NASDAQ_LIST_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"

INDEX_SYMBOL = "NDX"
STOCK_PCT_BASIS = ("Nasdaq latest reported move "
                   "(after-hours move after the close)")
INDEX_PCT_BASIS = "regular-session move vs previous close"
PRICE_BASIS = "nasdaq_last_sale"
FRESHNESS_BASIS = ("provider last-trade time when Nasdaq publishes one, "
                   "else collector receipt")
STALE_AFTER_SECONDS = 3600  # hourly cadence

# NYSE holidays, 2026 only. Verified against the NYSE calendar pattern for
# 2026 (Jul 3 is the observed Independence Day closure; Jul 4 is a
# Saturday). Other years are NOT asserted: see market_status().
HOLIDAYS_2026 = frozenset({
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving Day
    date(2026, 12, 25),  # Christmas Day
})


def market_status(now=None):
    """Equity session state for ``now`` (aware datetime, default UTC now).

    Returns ``{"status": "open"|"closed", "reason": ...}``. ``reason`` is
    one of ``in_session``, ``weekend``, ``holiday_2026``,
    ``outside_hours`` or ``outside_hours_holiday_calendar_unverified``
    (weekday in-hours for a year other than 2026, where no holiday list
    is asserted — the status is computed from weekday/hours only).
    """
    now = now or datetime.now(UTC)
    et_now = now.astimezone(ET)
    day = et_now.date()
    if et_now.weekday() >= 5:
        return {"status": "closed", "reason": "weekend"}
    if day.year == 2026 and day in HOLIDAYS_2026:
        return {"status": "closed", "reason": "holiday_2026"}
    if time(9, 30) <= et_now.time() < time(16, 0):
        reason = "in_session"
        if day.year != 2026:
            reason = "outside_hours_holiday_calendar_unverified"
        return {"status": "open", "reason": reason}
    return {"status": "closed", "reason": "outside_hours"}


def _money(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("$", "").replace(",", "")
    if text in ("", "UNCH", "N/A", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pct(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "")
    if text in ("", "UNCH", "N/A", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "N/A", "--"):
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_trade_ts(value):
    """Parse Nasdaq's ``lastTradeTimestamp`` to an aware UTC datetime.

    Full form: "Nov 23, 2026 6:38 PM ET" (optionally prefixed "Closed at ").
    Date-only form ("Nov 23, 2026", as published for NDX) has no usable
    time and returns None rather than inventing one.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().removeprefix("Closed at ").strip()
    for fmt in ("%b %d, %Y %I:%M %p ET", "%b %d, %Y %I:%M %p"):
        try:
            naive = datetime.strptime(text, fmt)
            return naive.replace(tzinfo=ET).astimezone(UTC)
        except ValueError:
            continue
    return None


def parse_nasdaq_quote(payload, symbol):
    """Parse one Nasdaq quote/info response into a normalized quote dict.

    Returns None when the payload carries no usable primary data (the
    caller records the symbol as source-unavailable). Day high/low come
    from ``keyStats.dayrange`` ("low - high"); ``previous_close`` is
    price minus net change; ``provider_observed_at`` is the parsed
    ``lastTradeTimestamp`` (None for date-only stamps). Market cap is NOT
    in this response — it comes from the list-type endpoint, see
    :func:`fetch_list_market_caps`.
    """
    data = (payload or {}).get("data") or {}
    primary = data.get("primaryData") or {}
    price = _money(primary.get("lastSalePrice"))
    if price is None:
        return None
    change = _money(primary.get("netChange"))
    day_high = _money(primary.get("dayHigh"))
    day_low = _money(primary.get("dayLow"))
    day_range = (data.get("keyStats") or {}).get("dayrange", {}).get("value")
    if (day_high is None or day_low is None) and isinstance(day_range, str):
        parts = [p.strip() for p in day_range.split("-")]
        if len(parts) == 2:
            day_low = day_low if day_low is not None else _money(parts[0])
            day_high = day_high if day_high is not None else _money(parts[1])
    observed = _parse_trade_ts(primary.get("lastTradeTimestamp"))
    return {
        "symbol": symbol,
        "price": price,
        "change": change,
        "percent_change": _pct(primary.get("percentageChange")),
        "market_cap": None,  # filled from the list-type endpoint in collect()
        "volume": _int(primary.get("volume")),
        "previous_close": (price - change) if change is not None else None,
        "day_high": day_high,
        "day_low": day_low,
        "provider_market_status": data.get("marketStatus"),
        "provider_observed_at": observed.isoformat() if observed else None,
        "provider_observed_label": primary.get("lastTradeTimestamp"),
    }


def fetch_list_market_caps(session):
    """Fetch per-listing market caps from Nasdaq's list-type endpoint.

    One request; the same official source as the embedded universe.
    Returns ``{symbol: market_cap}``. Raises on transport/HTTP errors;
    the caller leaves market caps (and ranks) null rather than guessing.
    """
    resp = session.get(NASDAQ_LIST_URL, timeout=20)
    resp.raise_for_status()
    rows = resp.json()["data"]["data"]["rows"]
    caps = {}
    for row in rows:
        cap = _money(row.get("marketCap"))
        if row.get("symbol") and cap is not None:
            caps[row["symbol"]] = cap
    return caps


def fetch_nasdaq_quote(symbol, session, asset_class="stocks"):
    """Fetch and parse one Nasdaq quote. Raises on transport/HTTP errors."""
    resp = session.get(NASDAQ_QUOTE_URL.format(symbol=symbol),
                       params={"assetclass": asset_class}, timeout=20)
    resp.raise_for_status()
    quote = parse_nasdaq_quote(resp.json(), symbol)
    if quote is None:
        raise ValueError(f"Nasdaq quote for {symbol} has no primary data")
    quote["fetched_at"] = datetime.now(UTC).isoformat()
    return quote


def _freshness(fetched_at, now, observed_at=None):
    """Freshness measured from the provider's last-trade time when known.

    ``observed_at`` is an ISO-8601 timestamp (or None); when absent the
    collector receipt time (``fetched_at``) is used instead. Either way the
    basis is stated, never implied.
    """
    observed = None
    if observed_at:
        try:
            observed = (datetime.fromisoformat(observed_at) if isinstance(observed_at, str)
                        else observed_at)
        except ValueError:
            observed = None
    anchor = observed or fetched_at
    age = max(0.0, (now - anchor).total_seconds()) if anchor else None
    return {"status": "fresh" if age is not None and age <= STALE_AFTER_SECONDS else "stale",
            "observed_at": anchor.isoformat() if anchor else None,
            "age_seconds": age,
            "stale_after_seconds": STALE_AFTER_SECONDS,
            "basis": FRESHNESS_BASIS}


def build_snapshot(quotes, index_quote, news_by_symbol=None,
                   candles_by_symbol=None, now=None):
    """Build the ``nasdaq/latest.json`` snapshot from fetched inputs.

    ``quotes`` maps symbol -> parsed Nasdaq quote dict or None (unavailable).
    ``index_quote`` is the parsed NDX quote or None. ``news_by_symbol``
    maps symbol -> headline list; ``candles_by_symbol`` maps symbol ->
    ``{"bars_1m": [...], "daily": [...]}`` for the 1h/7d changes.
    Pure apart from the clock: no network, no files.
    """
    now = now or datetime.now(UTC)
    # Read-time enforcement of the 12-hour publication window: anything
    # stored or passed in that is older than 12 hours (or undated) is
    # cycled out here, so nothing older is ever published.
    news_by_symbol = {symbol: news_mod.filter_recent(items, now)
                      for symbol, items in (news_by_symbol or {}).items()}
    candles_by_symbol = candles_by_symbol or {}
    name_by_symbol = names()
    validate()
    rows = []
    for symbol, _ in SYMBOLS:
        quote = quotes.get(symbol)
        candle_data = candles_by_symbol.get(symbol) or {}
        bars_1m = candle_data.get("bars_1m") or []
        daily = candle_data.get("daily") or []
        fetched_at = None
        if quote and quote.get("fetched_at"):
            try:
                fetched_at = datetime.fromisoformat(quote["fetched_at"])
            except ValueError:
                fetched_at = None
        if quote is None:
            rows.append({
                "symbol": symbol, "name": name_by_symbol.get(symbol),
                "price": None, "change": None, "percent_change": None,
                "percent_change_basis": STOCK_PCT_BASIS,
                "market_cap": None, "rank": None,
                "day_high": None, "day_low": None, "volume": None,
                "change_1h_pct": None, "change_7d_pct": None,
                "news": news_by_symbol.get(symbol) or [],
                "status": "source_unavailable",
                "price_basis": PRICE_BASIS,
                "provider_observed_at": None,
                "freshness": {"status": "unknown", "observed_at": None,
                              "age_seconds": None,
                              "stale_after_seconds": STALE_AFTER_SECONDS,
                              "basis": FRESHNESS_BASIS},
            })
            continue
        day_high = quote.get("day_high")
        day_low = quote.get("day_low")
        if (day_high is None or day_low is None) and daily:
            # Fall back to the latest Yahoo daily bar's range; the quote
            # endpoint's keyStats.dayrange is preferred when present.
            day_high = day_high if day_high is not None else daily[-1].get("high")
            day_low = day_low if day_low is not None else daily[-1].get("low")
        rows.append({
            "symbol": symbol, "name": name_by_symbol.get(symbol),
            "price": quote["price"], "change": quote["change"],
            "percent_change": quote["percent_change"],
            "percent_change_basis": STOCK_PCT_BASIS,
            "market_cap": quote.get("market_cap"), "rank": None,
            "day_high": day_high, "day_low": day_low,
            "volume": quote.get("volume"),
            "change_1h_pct": candles_mod.change_1h_pct(bars_1m, now) if bars_1m else None,
            "change_7d_pct": candles_mod.change_7d_pct(daily) if daily else None,
            "news": news_by_symbol.get(symbol) or [],
            "status": "available",
            "price_basis": PRICE_BASIS,
            "provider_observed_at": quote.get("provider_observed_at"),
            "freshness": _freshness(fetched_at, now,
                                    observed_at=quote.get("provider_observed_at")),
        })
    # Rank available stocks by market cap, largest first (1-based).
    ranked = sorted((r for r in rows if r["status"] == "available"
                     and r["market_cap"] is not None),
                    key=lambda r: r["market_cap"], reverse=True)
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i
    available = [r for r in rows if r["status"] == "available"]
    advancers = sum(1 for r in available if (r["percent_change"] or 0) > 0)
    decliners = sum(1 for r in available if (r["percent_change"] or 0) < 0)
    unchanged = len(available) - advancers - decliners
    by_pct = sorted((r for r in available if r["percent_change"] is not None),
                    key=lambda r: r["percent_change"], reverse=True)
    index = {"symbol": INDEX_SYMBOL, "status": "available" if index_quote else "source_unavailable",
             "level": index_quote["price"] if index_quote else None,
             "change": index_quote["change"] if index_quote else None,
             "percent_change": index_quote["percent_change"] if index_quote else None,
             "percent_change_basis": INDEX_PCT_BASIS,
             "previous_close": index_quote.get("previous_close") if index_quote else None,
             "day_high": index_quote.get("day_high") if index_quote else None,
             "day_low": index_quote.get("day_low") if index_quote else None,
             "market_status": market_status(now),
             "price_basis": PRICE_BASIS,
             "provider_observed_at": index_quote.get("provider_observed_at") if index_quote else None,
             "freshness": _freshness(
                 datetime.fromisoformat(index_quote["fetched_at"])
                 if index_quote and index_quote.get("fetched_at") else None, now,
                 observed_at=index_quote.get("provider_observed_at") if index_quote else None)
             if index_quote else {"status": "unknown", "observed_at": None,
                                  "age_seconds": None,
                                  "stale_after_seconds": STALE_AFTER_SECONDS,
                                  "basis": FRESHNESS_BASIS},
             "breadth": {"advancers": advancers, "decliners": decliners,
                         "unchanged": unchanged},
             "top_gainers": [{"symbol": r["symbol"], "percent_change": r["percent_change"]}
                             for r in by_pct[:5]],
             "top_losers": [{"symbol": r["symbol"], "percent_change": r["percent_change"]}
                            for r in by_pct[-5:][::-1]]}
    as_of = now.isoformat()
    return {
        "schema_version": 1,
        "dataset": "nasdaq_snapshot",
        "as_of": as_of,
        "generated_at": as_of,
        "universe": {"source": SOURCE_LABEL, "source_url": NASDAQ_LIST_URL,
                     "as_of": SOURCE_AS_OF, "listings": EXPECTED_LISTINGS,
                     "companies": 100},
        "index": index,
        "stocks": rows,
        "coverage": {
            "listings": EXPECTED_LISTINGS,
            "available": len(available),
            "source_unavailable": EXPECTED_LISTINGS - len(available),
            "with_news": sum(1 for r in rows if r["news"]),
            "with_candles": sum(1 for s in candles_by_symbol
                                if (candles_by_symbol[s] or {}).get("bars_1m")),
        },
        "sources": {
            "prices": "Nasdaq official quote API (api.nasdaq.com)",
            "candles": "Yahoo Finance chart API (query1.finance.yahoo.com)",
            "news": "Google News RSS (news.google.com)",
            "index": "Nasdaq official quote API (NDX)",
        },
        "session": {"model": "US equity session, weekdays 09:30-16:00 America/New_York",
                    "holiday_calendar": "NYSE holidays, 2026 only; other years not asserted",
                    "status": index["market_status"]},
        "notes": [
            "Per-stock percent_change is Nasdaq's latest reported move "
            "(after-hours move after the close); index percent_change is the "
            "regular-session move vs previous close. The two bases differ.",
            "News headlines are evidence only; they do not establish a cause "
            "for any price move.",
        ],
    }


def _session():
    import requests
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def collect(symbols=None, include_news=True, include_candles=True,
            max_workers=10, now=None, session=None):
    """Fetch everything and build the snapshot.

    Returns ``(snapshot, stats)`` where ``stats`` counts fetched/failed
    quotes, news and candles plus per-symbol errors. A failed source marks
    its symbols source-unavailable; it never aborts the whole collection.
    News is re-fetched fresh on every run (hourly, and on demand) and only
    headlines published within the last 12 hours are kept — see ``news.py``.
    """
    now = now or datetime.now(UTC)
    symbols = list(symbols) if symbols else [s for s, _ in SYMBOLS]
    own_session = session is None
    session = session or _session()
    stats = {"quotes_ok": 0, "quotes_failed": 0, "news_ok": 0,
             "news_failed": 0, "candles_ok": 0, "candles_failed": 0,
             "market_caps": 0, "errors": {}}
    try:
        quotes = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_safe_quote, session, s): s for s in symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                quote = future.result()
                if quote is None:
                    stats["quotes_failed"] += 1
                    stats["errors"][symbol] = "quote unavailable"
                else:
                    stats["quotes_ok"] += 1
                quotes[symbol] = quote
        try:
            index_quote = fetch_nasdaq_quote(INDEX_SYMBOL, session, asset_class="index")
        except Exception as exc:  # noqa: BLE001 - index failure is recorded, not fatal
            index_quote = None
            stats["errors"][INDEX_SYMBOL] = str(exc)
        news_by_symbol = {}
        if include_news:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_safe_news, session, s, now): s for s in symbols}
                for future in as_completed(futures):
                    symbol = futures[future]
                    items = future.result()
                    if items is None:
                        stats["news_failed"] += 1
                        stats["errors"].setdefault(symbol, "news unavailable")
                    else:
                        stats["news_ok"] += 1
                        news_by_symbol[symbol] = items
        candles_by_symbol = {}
        if include_candles:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_safe_candles, session, s): s for s in symbols}
                for future in as_completed(futures):
                    symbol = futures[future]
                    data = future.result()
                    if data is None:
                        stats["candles_failed"] += 1
                        stats["errors"].setdefault(symbol, "candles unavailable")
                    else:
                        stats["candles_ok"] += 1
                        candles_by_symbol[symbol] = data
        # Market caps come from the list-type endpoint (one request); the
        # quote endpoint does not publish them. A failed list fetch leaves
        # caps (and ranks) null rather than guessed.
        try:
            caps = fetch_list_market_caps(session)
            stats["market_caps"] = len(caps)
            for quote in quotes.values():
                if quote is not None:
                    quote["market_cap"] = caps.get(quote["symbol"])
        except Exception as exc:  # noqa: BLE001 - caps failure is recorded, not fatal
            stats["errors"]["__market_caps__"] = str(exc)
        snapshot = build_snapshot(quotes, index_quote, news_by_symbol,
                                  candles_by_symbol, now=now)
        return snapshot, stats
    finally:
        if own_session:
            session.close()


def _safe_quote(session, symbol):
    try:
        return fetch_nasdaq_quote(symbol, session)
    except Exception:  # noqa: BLE001 - per-symbol failure is recorded by caller
        return None


def _safe_news(session, symbol, now=None):
    try:
        return news_mod.fetch_news(symbol, session, now=now)
    except Exception:  # noqa: BLE001 - per-symbol failure is recorded by caller
        return None


def _safe_candles(session, symbol):
    try:
        bars = candles_mod.fetch_1m_bars(symbol, session)
        daily = candles_mod.fetch_daily_bars(symbol, session)
        return {"bars_1m": bars["bars"], "daily": daily["bars"]}
    except Exception:  # noqa: BLE001 - per-symbol failure is recorded by caller
        return None

