"""Offline tests for the NASDAQ-100 collector (part 1 of the panel PR).

No provider calls, no R2, no clock dependence beyond fixed fixtures:

    python -m pytest tests/test_nasdaq_collector.py

Covers the embedded universe (101 listings), the snapshot schema and its
two differently-labelled percentage bases, the provenance sidecar shape,
candle aggregation (including 9m bucketing from 1-minute bars), news
parsing/refresh and the equity session model.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_data.nasdaq_collector import candles as candles_mod  # noqa: E402
from market_data.nasdaq_collector import news as news_mod  # noqa: E402
from market_data.nasdaq_collector.collector import (  # noqa: E402
    INDEX_PCT_BASIS, STOCK_PCT_BASIS, build_snapshot, market_status,
    parse_nasdaq_quote,
)
from market_data.nasdaq_collector.provenance import build_provenance  # noqa: E402
from market_data.nasdaq_collector.publish import publish_snapshot  # noqa: E402
from market_data.nasdaq_collector.symbols import (  # noqa: E402
    DUAL_CLASS_PAIRS, EXPECTED_LISTINGS, SYMBOLS, names, symbols, validate,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)  # Monday, 10:00 ET, in session


def _quote(symbol, price, pct, cap, fetched_at=NOW):
    return {"symbol": symbol, "price": price, "change": round(price * pct / 100, 2),
            "percent_change": pct, "market_cap": cap, "volume": 1000,
            "previous_close": price, "day_high": price + 1, "day_low": price - 1,
            "fetched_at": fetched_at.isoformat()}


def _quotes():
    quotes = {}
    for i, symbol in enumerate(symbols()):
        # Alternate gainers/losers; AAPL largest cap, last symbol smallest.
        pct = 1.5 if i % 2 == 0 else -0.5
        quotes[symbol] = _quote(symbol, 100.0 + i, pct, (101 - i) * 10**9)
    return quotes


def _index():
    return _quote("NDX", 27000.0, 0.8, None)


# ---------------------------------------------------------------- universe

def test_universe_has_101_listings():
    assert len(SYMBOLS) == EXPECTED_LISTINGS == 101
    assert len(set(symbols())) == 101
    assert symbols() == sorted(symbols())


def test_universe_names_and_dual_class_pair():
    by_name = names()
    assert by_name["AAPL"].startswith("Apple")
    assert by_name["GOOGL"].startswith("Alphabet")
    assert by_name["GOOG"].startswith("Alphabet")
    assert DUAL_CLASS_PAIRS == (("GOOGL", "GOOG"),)
    # Only Alphabet has two listings: 101 listings, 100 distinct companies.
    companies = {name.split(" Common")[0].split(" Class")[0] for name in by_name.values()}
    assert len(companies) <= 101


def test_validate_fails_closed():
    validate()
    with pytest.raises(ValueError):
        validate(SYMBOLS[:100])
    with pytest.raises(ValueError):
        validate(SYMBOLS + (SYMBOLS[0],))


# ------------------------------------------------------- snapshot schema

def test_snapshot_schema_and_coverage():
    snap = build_snapshot(_quotes(), _index(), now=NOW)
    assert snap["schema_version"] == 1
    assert snap["dataset"] == "nasdaq_snapshot"
    assert snap["universe"]["listings"] == 101
    assert len(snap["stocks"]) == 101
    assert snap["coverage"] == {"listings": 101, "available": 101,
                                "source_unavailable": 0, "with_news": 0,
                                "with_candles": 0}
    aapl = next(r for r in snap["stocks"] if r["symbol"] == "AAPL")
    assert aapl["rank"] == 1  # largest market cap in the fixture
    assert aapl["status"] == "available"
    assert aapl["price_basis"] == "nasdaq_last_sale"
    assert aapl["provider_observed_at"] is None  # fixture quotes carry none
    assert aapl["freshness"]["status"] == "fresh"
    assert aapl["freshness"]["basis"].startswith("provider last-trade time")
    assert snap["index"]["level"] == 27000.0
    assert snap["index"]["market_status"] == {"status": "open", "reason": "in_session"}


def test_percent_change_bases_are_labelled_differently():
    snap = build_snapshot(_quotes(), _index(), now=NOW)
    stock = snap["stocks"][0]
    assert stock["percent_change_basis"] == STOCK_PCT_BASIS
    assert "after-hours" in stock["percent_change_basis"]
    assert snap["index"]["percent_change_basis"] == INDEX_PCT_BASIS
    assert "regular-session" in snap["index"]["percent_change_basis"]
    assert stock["percent_change_basis"] != snap["index"]["percent_change_basis"]


def test_breadth_and_top_movers():
    snap = build_snapshot(_quotes(), _index(), now=NOW)
    # Fixture: even indices +1.5%, odd indices -0.5% over 101 symbols.
    assert snap["index"]["breadth"] == {"advancers": 51, "decliners": 50, "unchanged": 0}
    gainers = snap["index"]["top_gainers"]
    assert len(gainers) == 5
    assert all(g["percent_change"] == 1.5 for g in gainers)
    losers = snap["index"]["top_losers"]
    assert len(losers) == 5
    assert all(g["percent_change"] == -0.5 for g in losers)


def test_source_unavailable_is_explicit_never_substituted():
    quotes = _quotes()
    quotes["AAPL"] = None
    quotes["MSFT"] = None
    snap = build_snapshot(quotes, _index(), now=NOW)
    aapl = next(r for r in snap["stocks"] if r["symbol"] == "AAPL")
    assert aapl["status"] == "source_unavailable"
    assert aapl["price"] is None and aapl["percent_change"] is None
    assert aapl["rank"] is None
    assert snap["coverage"]["available"] == 99
    assert snap["coverage"]["source_unavailable"] == 2
    assert snap["index"]["breadth"]["advancers"] + snap["index"]["breadth"]["decliners"] == 99


def test_missing_index_is_explicit():
    snap = build_snapshot(_quotes(), None, now=NOW)
    assert snap["index"]["status"] == "source_unavailable"
    assert snap["index"]["level"] is None
    assert snap["index"]["percent_change"] is None
    # Breadth still comes from the stocks, and the basis label stays put.
    assert snap["index"]["percent_change_basis"] == INDEX_PCT_BASIS
    assert snap["index"]["breadth"]["advancers"] == 51


def test_news_attached_with_source_link_and_time():
    items = [{"title": "AAPL unveils chip", "source": "Reuters",
              "link": "https://example.invalid/a", "published_at": "2026-11-23T12:00:00+00:00"}]
    snap = build_snapshot(_quotes(), _index(), news_by_symbol={"AAPL": items}, now=NOW)
    aapl = next(r for r in snap["stocks"] if r["symbol"] == "AAPL")
    assert aapl["news"] == items
    assert snap["coverage"]["with_news"] == 1


# ------------------------------------------------------------- nasdaq parse

def test_parse_nasdaq_quote_money_dayrange_and_trade_time():
    # Real quote/info shape (verified live 2026-11-23): primaryData has no
    # marketCap/previousClose/dayRange; day range lives in keyStats and the
    # last-trade timestamp in primaryData.lastTradeTimestamp.
    payload = {"data": {"marketStatus": "Market Open", "primaryData": {
        "lastSalePrice": "$348.85", "netChange": "$0.23",
        "percentageChange": "-0.07%",
        "volume": "12,345",
        "lastTradeTimestamp": "Nov 23, 2026 6:38 PM ET"},
        "keyStats": {"dayrange": {"label": "High/Low:", "value": "345.44 - 352.60"}}}}
    quote = parse_nasdaq_quote(payload, "AAPL")
    assert quote["price"] == 348.85
    assert quote["change"] == 0.23
    assert quote["percent_change"] == -0.07
    assert quote["previous_close"] == pytest.approx(348.85 - 0.23)
    assert quote["market_cap"] is None  # list-type endpoint only
    assert quote["volume"] == 12345
    assert quote["day_low"] == 345.44 and quote["day_high"] == 352.60
    assert quote["provider_observed_at"] == "2026-11-23T23:38:00+00:00"
    # Date-only stamps (as published for NDX) carry no usable time.
    assert parse_nasdaq_quote(
        {"data": {"primaryData": {"lastSalePrice": "26746.10",
                                  "lastTradeTimestamp": "Nov 23, 2026"}}},
        "NDX")["provider_observed_at"] is None


def test_parse_nasdaq_quote_unch_is_none_and_missing_is_none():
    payload = {"data": {"primaryData": {"lastSalePrice": "$10.00",
                                        "netChange": "UNCH", "percentageChange": "UNCH"}}}
    quote = parse_nasdaq_quote(payload, "X")
    assert quote["change"] is None and quote["percent_change"] is None
    assert parse_nasdaq_quote({"data": {}}, "X") is None
    assert parse_nasdaq_quote({}, "X") is None


# -------------------------------------------------------------- provenance

def test_provenance_shape_unpublished_then_published():
    snap = build_snapshot(_quotes(), _index(), now=NOW)
    prov = build_provenance(snap, publication_status="not_published",
                            publication_reason="local run: --publish not given")
    assert prov["schema"] == "snapshot_provenance.v1"
    assert prov["artifact"] == "nasdaq/latest.json"
    assert prov["constructed_at"] == snap["generated_at"]
    assert prov["archive_uploaded_at"] is None
    assert prov["latest_published_at"] is None
    assert prov["publication"] == {"status": "not_published",
                                   "reason": "local run: --publish not given"}
    assert prov["as_of"] == snap["as_of"]
    assert set(prov["stocks"]) == set(symbols())
    assert prov["stocks"]["AAPL"]["price_basis"] == "nasdaq_last_sale"
    assert prov["stocks"]["AAPL"]["freshness"]["status"] == "fresh"
    assert prov["index"]["symbol"] == "NDX"
    assert prov["coverage"]["available"] == 101
    published = build_provenance(snap, publication_status="published",
                                 latest_published_at="2026-11-23T15:01:00+00:00")
    assert published["publication"]["status"] == "published"
    assert published["latest_published_at"] == "2026-11-23T15:01:00+00:00"


def test_publish_writes_snapshot_then_sidecar_keys():
    class FakeS3:
        def __init__(self):
            self.puts = []

        def put_object(self, Bucket, Key, Body, **kwargs):
            self.puts.append((Bucket, Key, json.loads(Body)))

    snap = build_snapshot(_quotes(), _index(), now=NOW)
    prov = build_provenance(snap)
    fake = FakeS3()
    result = publish_snapshot(snap, prov,
                              config={"R2_ENDPOINT_URL": "https://example.invalid",
                                      "R2_ACCESS_KEY_ID": "x", "R2_SECRET_ACCESS_KEY": "y",
                                      "NASDAQ_R2_BUCKET": "bucket"},
                              client=fake)
    assert result["keys"] == ["nasdaq/latest.json", "nasdaq/latest.provenance.json"]
    assert [key for _, key, _ in fake.puts] == result["keys"]
    assert fake.puts[0][2]["dataset"] == "nasdaq_snapshot"
    assert fake.puts[1][2]["schema"] == "snapshot_provenance.v1"


# ------------------------------------------------------- candles: 9m et al

def _minute_bars(start_et, count, start_price=100.0):
    """Synthetic 1m bars starting at an aware ET datetime."""
    bars = []
    for i in range(count):
        t = start_et + timedelta(minutes=i)
        bars.append({"t": t, "open": start_price + i, "high": start_price + i + 0.5,
                     "low": start_price + i - 0.5, "close": start_price + i + 0.25,
                     "volume": 10})
    return bars


def test_aggregate_9m_buckets_from_1m_bars():
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    bars = _minute_bars(datetime(2026, 11, 23, 9, 30, tzinfo=et), 30)
    out = candles_mod.aggregate_bars(bars, "9m")
    # 30 minutes -> 09:30, 09:39, 09:48, 09:57 (9+9+9+3 bars).
    assert [b["start"] for b in out] == [
        "2026-11-23T09:30:00-05:00", "2026-11-23T09:39:00-05:00",
        "2026-11-23T09:48:00-05:00", "2026-11-23T09:57:00-05:00"]
    assert [b["bars"] for b in out] == [9, 9, 9, 3]
    assert out[0]["open"] == 100.0 and out[0]["close"] == 108.25
    assert out[0]["high"] == 108.5 and out[0]["low"] == 99.5
    assert out[0]["volume"] == 90
    assert out[-1]["bars"] == 3 and out[-1]["close"] == 129.25


def test_aggregate_2h_and_3m_buckets():
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    bars = _minute_bars(datetime(2026, 11, 23, 9, 30, tzinfo=et), 150)
    two_hour = candles_mod.aggregate_bars(bars, "2h")
    assert [b["start"] for b in two_hour] == ["2026-11-23T09:30:00-05:00",
                                              "2026-11-23T11:30:00-05:00"]
    assert [b["bars"] for b in two_hour] == [120, 30]
    three = candles_mod.aggregate_bars(bars[:10], "3m")
    assert [b["bars"] for b in three] == [3, 3, 3, 1]


def test_aggregate_drops_pre_market_bars():
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    bars = _minute_bars(datetime(2026, 11, 23, 8, 0, tzinfo=et), 120)
    out = candles_mod.aggregate_bars(bars, "9m")
    assert out[0]["start"] == "2026-11-23T09:30:00-05:00"
    assert sum(b["bars"] for b in out) == 30  # only 09:30-10:00 kept


def test_parse_yahoo_chart_skips_null_closes():
    payload = {"chart": {"result": [{"meta": {"symbol": "AAPL"},
                                     "timestamp": [1000, 1060, 1120],
                                     "indicators": {"quote": [{"open": [1, 2, 3],
                                                              "high": [1, 2, 3],
                                                              "low": [1, 2, 3],
                                                              "close": [1.5, None, 3.5],
                                                              "volume": [5, 6, 7]}]}}]}}
    parsed = candles_mod.parse_yahoo_chart(payload)
    assert [b["close"] for b in parsed["bars"]] == [1.5, 3.5]
    assert candles_mod.parse_yahoo_chart({}) == {"bars": [], "meta": {}}
    assert candles_mod.parse_yahoo_chart({"chart": {"result": []}})["bars"] == []


def test_change_1h_and_7d():
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    bars = _minute_bars(datetime(2026, 11, 23, 9, 30, tzinfo=et), 120, start_price=100.0)
    pct = candles_mod.change_1h_pct(bars, now=datetime(2026, 11, 23, 11, 30, tzinfo=et))
    # Last bar starts 11:29 (close 219.25); anchored to it, the 1h-ago
    # reference is the 10:29 bar (close 159.25).
    assert pct == pytest.approx((219.25 - 159.25) / 159.25 * 100)
    assert candles_mod.change_1h_pct(bars[:30]) is None  # under an hour of bars
    # Regression: after the close, wall-clock now is hours past the last
    # bar; anchoring to now would compare the last close with itself (0%).
    late_now = datetime(2026, 11, 23, 23, 30, tzinfo=et)
    assert candles_mod.change_1h_pct(bars, now=late_now) == pytest.approx(
        (219.25 - 159.25) / 159.25 * 100)
    daily = [{"close": 90.0}, {"close": 95.0}, {"close": 100.0},
             {"close": 105.0}, {"close": 110.0}, {"close": 115.0},
             {"close": 120.0}]
    assert candles_mod.change_7d_pct(daily) == pytest.approx((120 - 95) / 95 * 100)
    assert candles_mod.change_7d_pct([{"close": 100.0}]) is None


# ------------------------------------------------------------------- news

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>test</title>
<item><title>Apple unveils chip &amp; more</title><link>https://example.invalid/a</link>
<source url="https://example.invalid">Reuters</source>
<pubDate>Mon, 23 Nov 2026 12:00:00 GMT</pubDate></item>
<item><title>Second headline</title><link>https://example.invalid/b</link>
<pubDate>Mon, 23 Nov 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_news_rss_parsing():
    items = news_mod.parse_rss(RSS)
    assert len(items) == 2
    assert items[0] == {"title": "Apple unveils chip & more", "source": "Reuters",
                        "link": "https://example.invalid/a",
                        "published_at": "2026-11-23T12:00:00+00:00"}
    assert items[1]["source"] is None
    assert items[1]["published_at"] == "2026-11-23T11:00:00+00:00"
    assert news_mod.parse_rss("not xml") == []
    assert news_mod.news_url("AAPL").startswith("https://news.google.com/rss/search?q=AAPL+stock")


def test_news_refresh_after_12h():
    assert news_mod.should_refresh(None) is True
    assert news_mod.should_refresh("2026-11-23T03:00:00+00:00",
                                   now=datetime(2026, 11, 23, 15, 0, tzinfo=UTC)) is False
    assert news_mod.should_refresh("2026-11-23T02:59:00+00:00",
                                   now=datetime(2026, 11, 23, 15, 0, tzinfo=UTC)) is True
    assert news_mod.should_refresh("garbage") is True


# ---------------------------------------------------------------- session

def test_market_status_session_model():
    assert market_status(datetime(2026, 11, 23, 15, 0, tzinfo=UTC)) == {"status": "open", "reason": "in_session"}  # Mon 10:00 ET
    assert market_status(datetime(2026, 11, 23, 13, 0, tzinfo=UTC))["status"] == "closed"  # Mon 08:00 ET
    assert market_status(datetime(2026, 11, 21, 15, 0, tzinfo=UTC)) == {"status": "closed", "reason": "weekend"}  # Sat
    assert market_status(datetime(2026, 11, 26, 15, 0, tzinfo=UTC)) == {"status": "closed", "reason": "holiday_2026"}  # Thanksgiving
    assert market_status(datetime(2026, 11, 23, 21, 0, tzinfo=UTC))["status"] == "closed"  # Mon 16:00 ET, close
