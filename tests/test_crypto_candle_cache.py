"""Offline tests for the crypto candle cache.

No provider calls, no R2, no clock dependence beyond fixed fixtures::

    python -m pytest tests/test_crypto_candle_cache.py -q
    python -m unittest tests.test_crypto_candle_cache -v

Covers the embedded universe (50 coins), the three source clients
(Coinbase row order and 300-bar paging walk, Tiingo bar shape and
missing volume, CoinGecko OHLC shape and daily-only base),
24/7 UTC resampling (9m buckets from 00:00 UTC, 2h/4h UTC-day
groups, weekly volume sums, absent intraday for CoinGecko coins),
the cache reader/writer (envelope, merge conflicts, validation),
the One Clock port (bar-to-observation mapping, forming bars,
as-of selection, revision rules, conflict error, reading states,
gap detection, coverage labels), the provenance sidecar, publish
keys, pacing/checkpoint, and the collect/update flows with fake
clients — plus the checked-in BTC seed (real Coinbase bars).
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_data.crypto_candle_cache import (  # noqa: E402
    cache as cache_mod,
)
from market_data.crypto_candle_cache import (  # noqa: E402
    coins as coins_mod,
)
from market_data.crypto_candle_cache import (  # noqa: E402
    collector as collector_mod,
)
from market_data.crypto_candle_cache import (  # noqa: E402
    one_clock as oc,
)
from market_data.crypto_candle_cache import (  # noqa: E402
    provenance as prov_mod,
)
from market_data.crypto_candle_cache import (  # noqa: E402
    resample as resample_mod,
)
from market_data.crypto_candle_cache.coingecko import (  # noqa: E402
    CoinGeckoClient, parse_ohlc_row,
)
from market_data.crypto_candle_cache.coinbase import (  # noqa: E402
    CoinbaseClient, parse_candle_row,
)
from market_data.crypto_candle_cache.pacing import (  # noqa: E402
    CheckpointStore, RateLimiter,
)
from market_data.crypto_candle_cache.publish import (  # noqa: E402
    publish_symbol, read_config,
)
from market_data.crypto_candle_cache.tiingo import (  # noqa: E402
    TiingoCryptoClient, TiingoError, parse_crypto_bar,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 24, 0, 30, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
FETCHED_AT = "2026-11-24T00:30:00Z"


def bar(t, open_=100.0, high=101.0, low=99.0, close=100.5, volume=None,
        observed_at=FETCHED_AT, origin="recorded"):
    """A test bar with the One Clock envelope attached."""
    out = {"t": t, "open": open_, "high": high, "low": low, "close": close,
           "observed_at": observed_at, "origin": origin}
    if volume is not None:
        out["volume"] = volume
    return out


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


class FakeCoinbaseSession:
    """Emulates Coinbase paging: newest 300 bars in [start, end)."""

    def __init__(self, bars_by_product):
        # bars_by_product: {product: [bar-dicts ascending by t]}
        self.bars_by_product = bars_by_product
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        parsed = urlparse(url)
        product = parsed.path.split("/")[2]
        query = parse_qs(parsed.query)
        start = query["start"][0]
        end = query["end"][0]
        rows = []
        for b in self.bars_by_product.get(product, []):
            if start <= b["t"] < end:
                epoch = int(datetime.fromisoformat(
                    b["t"].replace("Z", "+00:00")).timestamp())
                rows.append([epoch, b["low"], b["high"], b["open"],
                             b["close"], b.get("volume", 0.0)])
        rows.sort(reverse=True)
        return FakeResponse(200, rows[:300])


class FakeCoinGeckoSession:
    def __init__(self, rows_by_id):
        self.rows_by_id = rows_by_id
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        coin_id = urlparse(url).path.split("/")[4]
        return FakeResponse(200, self.rows_by_id.get(coin_id, []))


class FakeTiingoSession:
    def __init__(self, rows_by_ticker):
        self.rows_by_ticker = rows_by_ticker
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        ticker = parse_qs(urlparse(url).query)["tickers"][0]
        return FakeResponse(200, self.rows_by_ticker.get(ticker, []))


def minute_bars(start, count, start_price=60000.0):
    """Synthetic 1-minute bars from ``start`` (datetime), ascending."""
    bars = []
    price = start_price
    for i in range(count):
        moment = start + timedelta(minutes=i)
        bars.append(bar(moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        open_=price, high=price + 10, low=price - 10,
                        close=price + 1, volume=1.5))
        price += 1
    return bars


class UniverseTest(unittest.TestCase):
    def test_counts_and_labels(self):
        coins_mod.validate()
        self.assertEqual(len(coins_mod.COINS), 50)
        self.assertEqual(len(coins_mod.COINBASE_COINS), 32)
        self.assertEqual(len(coins_mod.COINGECKO_COINS), 18)

    def test_added_eleven(self):
        by = coins_mod.BY_SYMBOL
        self.assertEqual(by["USDS"].source, "coinbase")
        self.assertEqual(by["USDS"].coinbase_product, "USDS-USD")
        self.assertEqual(by["USD1"].source, "coinbase")
        self.assertEqual(by["USD1"].coingecko_id, "usd1-wlfi")
        for symbol in ("USDE", "RLUSD", "USDG", "PYUSD", "RAIN", "M",
                       "OKB", "USDF", "U"):
            self.assertEqual(by[symbol].source, "coingecko", symbol)
            self.assertIsNone(by[symbol].coinbase_product, symbol)
        self.assertEqual(by["GRAM"].source, "coingecko")
        self.assertEqual(by["GRAM"].coingecko_id, "the-open-network")

    def test_ranks_sorted(self):
        ranks = [c.rank for c in coins_mod.COINS]
        self.assertEqual(ranks, sorted(ranks))


class CoinbaseTest(unittest.TestCase):
    def test_row_order_is_low_high_open_close(self):
        # [epoch, low, high, open, close, volume]
        parsed = parse_candle_row([1795478400, 61023.23, 61243.17,
                                   61190.81, 61204.79, 123.16])
        self.assertEqual(parsed, {"t": "2026-11-24T00:00:00Z", "open": 61190.81,
                                  "high": 61243.17, "low": 61023.23,
                                  "close": 61204.79, "volume": 123.16})

    def test_fetch_window_returns_oldest_first(self):
        bars = minute_bars(datetime(2026, 11, 23, 23, 0, tzinfo=UTC), 10)
        session = FakeCoinbaseSession({"BTC-USD": bars})
        client = CoinbaseClient(session=session, delay_seconds=0)
        got, made = client.fetch_window(
            "BTC-USD", "1m", datetime(2026, 11, 23, 23, 0, tzinfo=UTC),
            datetime(2026, 11, 23, 23, 10, tzinfo=UTC))
        self.assertEqual(made, 1)
        self.assertEqual([b["t"] for b in got],
                         [b["t"] for b in bars])
        self.assertEqual(got[0]["volume"], 1.5)

    def test_paging_walk_merges_deduped(self):
        # 648 hourly bars: two full pages (300 + 300) plus a 48 tail.
        bars = []
        start = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        for i in range(648):
            moment = start + timedelta(hours=i)
            bars.append(bar(moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            open_=100 + i, high=101 + i, low=99 + i,
                            close=100.5 + i, volume=2.0))
        session = FakeCoinbaseSession({"BTC-USD": bars})
        client = CoinbaseClient(session=session, delay_seconds=0)
        got, made = client.fetch_history(
            "BTC-USD", "1h", datetime(2026, 10, 28, 0, 0, tzinfo=UTC),
            datetime(2026, 11, 24, 0, 0, tzinfo=UTC))
        self.assertEqual(len(got), 648)
        self.assertEqual(made, 3)
        self.assertEqual(got[0]["t"], "2026-10-28T00:00:00Z")
        self.assertEqual(got[-1]["t"], "2026-11-23T23:00:00Z")
        self.assertEqual(len({b["t"] for b in got}), 648)

    def test_page_windows_never_exceed_the_300_bar_cap(self):
        # Coinbase answers a window holding more than 300 bars with
        # HTTP 400 instead of truncating (caught live 2026-11-24: a
        # 1-minute request over 24 hours failed outright). The walk
        # must therefore page in capped windows from the start.
        bars = minute_bars(datetime(2026, 11, 22, 0, 0, tzinfo=UTC), 2880)
        session = FakeCoinbaseSession({"BTC-USD": bars})
        client = CoinbaseClient(session=session, delay_seconds=0)
        got, made = client.fetch_history(
            "BTC-USD", "1m", datetime(2026, 11, 22, 0, 0, tzinfo=UTC),
            datetime(2026, 11, 24, 0, 0, tzinfo=UTC))
        self.assertEqual(len(got), 2880)
        self.assertEqual(made, 10)
        for url in session.calls:
            query = parse_qs(urlparse(url).query)
            start = datetime.fromisoformat(query["start"][0].replace("Z", "+00:00"))
            end = datetime.fromisoformat(query["end"][0].replace("Z", "+00:00"))
            self.assertLessEqual((end - start).total_seconds(), 300 * 60)


class TiingoTest(unittest.TestCase):
    def test_parse_bar_with_volume(self):
        parsed = parse_crypto_bar({"date": "2026-11-23T20:00:00.000Z",
                                   "open": 61000, "high": 61200,
                                   "low": 60900, "close": 61100,
                                   "volume": 12.5})
        self.assertEqual(parsed["t"], "2026-11-23T20:00:00Z")
        self.assertEqual(parsed["volume"], 12.5)

    def test_parse_bar_without_volume_has_no_volume_key(self):
        parsed = parse_crypto_bar({"date": "2026-11-23T20:00:00Z",
                                   "open": 61000, "high": 61200,
                                   "low": 60900, "close": 61100})
        self.assertNotIn("volume", parsed)

    def test_no_token_raises_before_any_request(self):
        session = FakeTiingoSession({})
        client = TiingoCryptoClient(token=None, session=session)
        # Force the no-token path: an empty-string token counts as absent.
        client.token = None
        with self.assertRaises(TiingoError):
            client.fetch_bars("BTC", "1h", "2026-11-20", "2026-11-23")
        self.assertEqual(session.calls, [])

    def test_fetch_bars(self):
        rows = [{"date": "2026-11-23T20:00:00.000Z", "open": 1, "high": 2,
                 "low": 0.5, "close": 1.5, "volume": 3}]
        session = FakeTiingoSession({"btcusd": rows})
        client = TiingoCryptoClient(token="test-token", session=session,
                                    delay_seconds=0)
        bars, made = client.fetch_bars("BTC", "1h", "2026-11-23", "2026-11-23")
        self.assertEqual(made, 1)
        self.assertEqual(bars[0]["close"], 1.5)
        self.assertIn("token=test-token", session.calls[0])
        self.assertIn("tickers=btcusd", session.calls[0])


class CoinGeckoTest(unittest.TestCase):
    def test_parse_ohlc_row_has_no_volume(self):
        # 2026-11-17T00:00:00Z in epoch ms (a daily bar: midnight UTC).
        parsed = parse_ohlc_row([1794873600000, 64841.0, 65087.0, 64362.0, 65003.0])
        self.assertEqual(parsed["t"], "2026-11-17T00:00:00Z")
        self.assertEqual(parsed["close"], 65003.0)
        self.assertNotIn("volume", parsed)

    def test_fetch_daily_dedupes(self):
        rows = [[1794873600000, 1, 2, 0.5, 1.5],
                [1794873600000, 1, 2, 0.5, 1.5],
                [1794960000000, 1, 2, 0.5, 1.5]]
        session = FakeCoinGeckoSession({"bitcoin": rows})
        client = CoinGeckoClient(session=session, delay_seconds=0)
        bars, made = client.fetch_daily("bitcoin")
        self.assertEqual(made, 1)
        self.assertEqual(len(bars), 2)
        self.assertIn("days=365", session.calls[0])


class ResampleTest(unittest.TestCase):
    def test_9m_buckets_align_to_utc_midnight_not_equity_open(self):
        # 20 one-minute bars from 00:00 UTC: 9m buckets are
        # 00:00-00:09, 00:09-00:18, then a 2-bar tail — never 09:30.
        bars = minute_bars(datetime(2026, 11, 23, 0, 0, tzinfo=UTC), 20)
        out = resample_mod.aggregate_intraday(bars, "9m")
        self.assertEqual([b["t"] for b in out],
                         ["2026-11-23T00:00:00Z", "2026-11-23T00:09:00Z",
                          "2026-11-23T00:18:00Z"])
        self.assertEqual([b["bars"] for b in out], [9, 9, 2])
        self.assertEqual(out[0]["open"], 60000.0)
        self.assertEqual(out[0]["close"], 60008 + 1)
        self.assertEqual(out[0]["volume"], 9 * 1.5)

    def test_2h_groups_within_utc_day(self):
        bars = []
        for i in range(5):  # 22:00, 23:00, 00:00, 01:00, 02:00 UTC
            moment = datetime(2026, 11, 23, 22, 0, tzinfo=UTC) + timedelta(hours=i)
            bars.append(bar(moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            open_=100 + i, high=101 + i, low=99 + i,
                            close=100.5 + i, volume=2.0))
        out = resample_mod.aggregate_hourly(bars, "2h")
        self.assertEqual([b["t"] for b in out],
                         ["2026-11-23T22:00:00Z", "2026-11-24T00:00:00Z",
                          "2026-11-24T02:00:00Z"])
        self.assertEqual([b["bars"] for b in out], [2, 2, 1])

    def test_weekly_sums_volume_and_keeps_partial_week(self):
        bars = []
        for i in range(10):  # Mon 2026-11-16 .. Wed 2026-11-25
            moment = datetime(2026, 11, 16, tzinfo=UTC) + timedelta(days=i)
            bars.append(bar(moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            open_=100 + i, high=102 + i, low=98 + i,
                            close=101 + i, volume=10.0))
        out = resample_mod.aggregate_weekly(bars)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["bars"], 7)
        self.assertEqual(out[0]["volume"], 70.0)
        self.assertEqual(out[1]["bars"], 3)
        self.assertEqual(out[1]["t"], "2026-11-23T00:00:00Z")

    def test_derive_all_coingecko_coin_has_only_daily_and_weekly(self):
        daily = [bar((datetime(2026, 11, 16, tzinfo=UTC) + timedelta(days=i)
                      ).strftime("%Y-%m-%dT%H:%M:%SZ"), volume=None)
                 for i in range(8)]
        out = resample_mod.derive_all({"1d": daily})
        self.assertEqual(sorted(out), ["1d", "1w"])
        self.assertNotIn("volume", out["1w"][0])

    def test_derive_all_keeps_bases_exact(self):
        bases = {"1m": minute_bars(datetime(2026, 11, 23, 0, 0, tzinfo=UTC), 30),
                 "1h": [], "1d": []}
        out = resample_mod.derive_all(bases)
        self.assertEqual(len(out["1m"]), 30)
        self.assertEqual(len(out["30m"]), 1)


class CacheTest(unittest.TestCase):
    def test_build_and_read_round_trip(self):
        bars = cache_mod.envelope_bars(
            minute_bars(datetime(2026, 11, 23, 0, 0, tzinfo=UTC), 5),
            FETCHED_AT, "recorded")
        content = cache_mod.build_interval_file("BTC", "1m", bars, "coinbase",
                                                FETCHED_AT)
        self.assertEqual(content["schema_version"], 1)
        self.assertEqual(content["dataset"], "crypto_candles")
        self.assertTrue(content["base"])
        self.assertIsNone(content["derived_from"])
        self.assertEqual(content["bars_count"], 5)
        self.assertEqual(content["bars"][0]["origin"], "recorded")
        self.assertEqual(content["bars"][0]["observed_at"], FETCHED_AT)

    def test_derived_file_names_its_base(self):
        derived = resample_mod.aggregate_intraday(
            minute_bars(datetime(2026, 11, 23, 0, 0, tzinfo=UTC), 10), "5m")
        enveloped = cache_mod.envelope_bars(derived, FETCHED_AT, "reconstructed")
        content = cache_mod.build_interval_file("BTC", "5m", enveloped, "coinbase",
                                                FETCHED_AT)
        self.assertFalse(content["base"])
        self.assertEqual(content["derived_from"], "1m")
        self.assertEqual(content["bars"][0]["bars"], 5)

    def test_read_rejects_bad_schema_and_unsorted_bars(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1m.json"
            bars = cache_mod.envelope_bars(
                minute_bars(datetime(2026, 11, 23, 0, 0, tzinfo=UTC), 3),
                FETCHED_AT, "recorded")
            content = cache_mod.build_interval_file("BTC", "1m", bars, "coinbase",
                                                    FETCHED_AT)
            path.write_text(json.dumps(content), encoding="utf-8")
            self.assertEqual(cache_mod.read_interval_file(path)["bars_count"], 3)
            content["schema_version"] = 2
            path.write_text(json.dumps(content), encoding="utf-8")
            with self.assertRaises(ValueError):
                cache_mod.read_interval_file(path)
            content["schema_version"] = 1
            content["bars"] = list(reversed(content["bars"]))
            path.write_text(json.dumps(content), encoding="utf-8")
            with self.assertRaises(ValueError):
                cache_mod.read_interval_file(path)

    def test_validate_bar_rejects_inconsistent_high_low(self):
        bad = bar("2026-11-23T00:00:00Z", open_=100, high=99, low=99, close=100)
        with self.assertRaises(ValueError):
            cache_mod.validate_bar(bad, "1m")

    def test_merge_conflict_raises_without_force(self):
        stored = [bar("2026-11-23T00:00:00Z", close=100.5)]
        changed = [bar("2026-11-23T00:00:00Z", close=99.5)]
        with self.assertRaises(ValueError):
            cache_mod.merge_bars(stored, changed, "1m")
        merged = cache_mod.merge_bars(stored, changed, "1m", force=True)
        self.assertEqual(merged[0]["close"], 99.5)
        same = [bar("2026-11-23T00:01:00Z", close=100.5)]
        merged = cache_mod.merge_bars(stored, same, "1m")
        self.assertEqual(len(merged), 2)


class OneClockTest(unittest.TestCase):
    def test_bar_to_observation_mapping(self):
        b = bar("2026-11-23T20:00:00Z", volume=3.0)
        row = oc.bar_to_observation(b, "BTC", "1h", "crypto/candles/BTC/1h.json",
                                    provider="coinbase")
        self.assertEqual(row["source"], "crypto_candle_cache")
        self.assertEqual(row["instrument"], "BTC")
        self.assertEqual(row["session"], "continuous")
        self.assertEqual(row["id"], "BTC|1h|2026-11-23T20:00:00Z")
        self.assertEqual(row["origin"], "recorded")
        # eventAt is the bar's close; valueAt is the bar's start.
        self.assertEqual(row["eventAt"], oc.instant_ms("2026-11-23T21:00:00Z"))
        self.assertEqual(row["valueAt"], oc.instant_ms("2026-11-23T20:00:00Z"))
        self.assertEqual(row["availableAt"], oc.instant_ms(FETCHED_AT))
        self.assertEqual(row["value"]["volume"], 3.0)
        self.assertEqual(row["provider"], "coinbase")

    def test_forming_bar_is_not_an_observation(self):
        forming = bar("2026-11-24T00:30:00Z")
        completed, forming_out = oc.split_forming([forming], "1m", NOW_MS)
        self.assertEqual(completed, [])
        self.assertEqual(len(forming_out), 1)
        # A forming bar passed straight to the mapper is rejected:
        # its availability precedes its close.
        with self.assertRaises(TypeError):
            oc.bar_to_observation(forming, "BTC", "1m",
                                  "crypto/candles/BTC/1m.json")

    def test_bar_observed_before_close_stays_forming_at_later_cutoff(self):
        # Fetched at 00:30, before the 00:00 bar closed at 01:00: the
        # bar is forming at 06:00 too — it needs a later fetch that
        # observes it after its close to become an observation.
        early = bar("2026-11-24T00:00:00Z")
        completed, forming = oc.split_forming(
            [early], "1h", oc.instant_ms("2026-11-24T06:00:00Z"))
        self.assertEqual(completed, [])
        self.assertEqual(len(forming), 1)
        rows, forming_out = oc.to_observations(
            [early], "BTC", "1h", "k",
            now_ms=oc.instant_ms("2026-11-24T06:00:00Z"))
        self.assertEqual(rows, [])
        self.assertEqual(len(forming_out), 1)

    def test_select_as_of_cutoff_and_availability(self):
        # Bars fetched at 22:15, after the 20:00 and 21:00 bars closed.
        bars = [bar("2026-11-23T20:00:00Z", observed_at="2026-11-23T22:15:00Z"),
                bar("2026-11-23T21:00:00Z", observed_at="2026-11-23T22:15:00Z"),
                bar("2026-11-23T22:00:00Z", observed_at="2026-11-23T22:15:00Z")]
        rows, _ = oc.to_observations(
            bars, "BTC", "1h", "crypto/candles/BTC/1h.json",
            now_ms=oc.instant_ms("2026-11-23T22:30:00Z"))
        selected = oc.select_as_of(rows, {"session": "continuous",
                                          "instrument": "BTC",
                                          "cutoff": "2026-11-23T22:30:00Z"})
        self.assertEqual([r["id"].split("|")[-1] for r in selected],
                         ["2026-11-23T20:00:00Z", "2026-11-23T21:00:00Z"])
        # A cutoff before any bar's availability selects nothing.
        self.assertEqual(oc.select_as_of(
            rows, {"session": "continuous", "instrument": "BTC",
                   "cutoff": "2026-11-23T20:30:00Z"}), [])
        # Wrong session or origin selects nothing.
        self.assertEqual(oc.select_as_of(
            rows, {"session": "regular", "instrument": "BTC",
                   "cutoff": "2026-11-23T22:30:00Z"}), [])
        self.assertEqual(oc.select_as_of(
            rows, {"session": "continuous", "instrument": "BTC",
                   "cutoff": "2026-11-23T22:30:00Z", "origin": "reconstructed"}), [])

    def test_select_as_of_highest_revision_wins_and_conflict_raises(self):
        base = bar("2026-11-23T20:00:00Z")
        old = oc.bar_to_observation(base, "BTC", "1h", "k", revision=1)
        new = oc.bar_to_observation(
            bar("2026-11-23T20:00:00Z", close=102.0), "BTC", "1h", "k",
            revision=2)
        selected = oc.select_as_of(
            [old, new], {"session": "continuous", "instrument": "BTC",
                         "cutoff": "2026-11-24T00:30:00Z"})
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["revision"], 2)
        self.assertEqual(selected[0]["value"]["close"], 102.0)
        clash = oc.bar_to_observation(
            bar("2026-11-23T20:00:00Z", close=103.0), "BTC", "1h", "k",
            revision=1)
        with self.assertRaises(ValueError):
            oc.select_as_of(
                [old, clash], {"session": "continuous", "instrument": "BTC",
                               "cutoff": "2026-11-24T00:30:00Z"})

    def test_reading_states(self):
        # Observed exactly at the bar's close: availableAt == eventAt
        # is allowed, and the bar is selectable at any later cutoff.
        bars = [bar("2026-11-23T20:00:00Z", observed_at="2026-11-23T21:00:00Z")]
        rows, _ = oc.to_observations(
            bars, "BTC", "1h", "k", now_ms=oc.instant_ms("2026-11-24T00:30:00Z"))
        fresh = oc.reading_as_of(
            rows, {"session": "continuous", "instrument": "BTC",
                   "cutoff": "2026-11-23T22:00:00Z"}, max_age_ms=2 * 3600 * 1000)
        self.assertEqual(fresh["state"], "fresh")
        stale = oc.reading_as_of(
            rows, {"session": "continuous", "instrument": "BTC",
                   "cutoff": "2026-11-23T22:00:00Z"}, max_age_ms=30 * 60 * 1000)
        self.assertEqual(stale["state"], "stale")
        self.assertEqual(stale["ageMs"], 2 * 60 * 60 * 1000)
        empty = oc.reading_as_of(
            [], {"session": "continuous", "instrument": "BTC",
                 "cutoff": "2026-11-23T21:00:00Z"}, max_age_ms=1000)
        self.assertEqual(empty["state"], "unavailable")
        with self.assertRaises(TypeError):
            oc.reading_as_of(rows, {"session": "continuous", "instrument": "BTC",
                                    "cutoff": "2026-11-23T21:00:00Z"},
                             max_age_ms=None)

    def test_detect_gaps_and_coverage(self):
        bars = [bar("2026-11-23T20:00:00Z"), bar("2026-11-23T21:00:00Z"),
                bar("2026-11-23T23:00:00Z")]  # 22:00 missing
        gaps = oc.detect_gaps(bars, "1h")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["after"], "2026-11-23T21:00:00Z")
        self.assertEqual(gaps[0]["missing_bars"], 1)
        label = oc.coverage_label(
            bars, "1h", oc.instant_ms("2026-11-23T20:00:00Z"),
            oc.instant_ms("2026-11-24T00:00:00Z"))
        self.assertEqual(label["label"], "coverage partial")
        full = oc.coverage_label(
            [bar("2026-11-23T20:00:00Z"), bar("2026-11-23T21:00:00Z"),
             bar("2026-11-23T22:00:00Z"), bar("2026-11-23T23:00:00Z")],
            "1h", oc.instant_ms("2026-11-23T20:00:00Z"),
            oc.instant_ms("2026-11-24T00:00:00Z"))
        self.assertEqual(full["label"], "coverage full")
        self.assertEqual(oc.coverage_label([], "1h", 0, 1)["label"],
                         "coverage partial")

    def test_instant_rejects_naive_and_seconds(self):
        with self.assertRaises(TypeError):
            oc.instant_ms("2026-11-23 20:00:00")
        with self.assertRaises(TypeError):
            oc.instant_ms(1795478400)


class ProvenanceTest(unittest.TestCase):
    def test_sidecar_shape_and_absent_intervals(self):
        files = {
            "1d": {"bars": 90, "first_bar": "2026-08-26T00:00:00Z",
                   "last_bar": "2026-11-23T00:00:00Z", "derived_from": None,
                   "source": "coingecko", "requests": 1, "coverage": "full"},
            "1w": {"bars": 13, "first_bar": "2026-08-24T00:00:00Z",
                   "last_bar": "2026-11-23T00:00:00Z", "derived_from": "1d",
                   "source": "coingecko", "requests": 0, "coverage": "full"},
            "_absent": {"1m": "CoinGecko free OHLC has no 1m/1h series",
                        "1h": "CoinGecko free OHLC has no 1m/1h series"},
        }
        sidecar = prov_mod.build_provenance(
            "TRX", files, generated_at=FETCHED_AT,
            publication_status="not_published",
            publication_reason="local run: --publish not given")
        self.assertEqual(sidecar["schema"], "snapshot_provenance.v1")
        self.assertEqual(sidecar["session_model"], "continuous_24_7")
        self.assertEqual(sidecar["files"]["1m"]["coverage"], "absent")
        self.assertEqual(sidecar["coverage"]["intervals"], 2)
        self.assertEqual(sidecar["coverage"]["bars_total"], 103)


class PublishTest(unittest.TestCase):
    def test_missing_config_raises(self):
        with self.assertRaises(RuntimeError):
            read_config(env={})

    def test_sidecar_uploaded_last(self):
        calls = []

        class FakeClient:
            def put_object(self, Bucket, Key, Body, ContentType):
                calls.append(Key)

        files = {"1d": {"bars": []}}
        provenance = {"schema": "snapshot_provenance.v1"}
        result = publish_symbol("BTC", files, provenance,
                                config={"R2_ENDPOINT_URL": "https://x",
                                        "R2_ACCESS_KEY_ID": "id",
                                        "R2_SECRET_ACCESS_KEY": "secret",
                                        "CRYPTO_R2_BUCKET": "bucket"},
                                client=FakeClient())
        self.assertEqual(result["bucket"], "bucket")
        self.assertEqual(calls, ["crypto/candles/BTC/1d.json",
                                 "crypto/candles/BTC/provenance.json"])


class PacingTest(unittest.TestCase):
    def test_tiingo_hourly_budget_blocks(self):
        now = [datetime(2026, 11, 23, 12, 0, tzinfo=UTC)]
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += timedelta(seconds=seconds)

        limiter = RateLimiter(
            log={"tiingo": [(now[0] - timedelta(minutes=i)).isoformat()
                            for i in range(50)]},
            sleep=fake_sleep, now=lambda: now[0])
        limiter.acquire("tiingo")
        self.assertTrue(sleeps)
        self.assertEqual(len(limiter.log["tiingo"]), 51)

    def test_checkpoint_round_trip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp) / ".cache-checkpoint.json")
            store.record_base("BTC", "1d", "complete", bars=90,
                              first_bar="2026-08-26T00:00:00Z",
                              last_bar="2026-11-23T00:00:00Z", requests=1)
            self.assertTrue(store.completed("BTC", "1d"))
            self.assertFalse(store.completed("BTC", "1h"))
            reloaded = CheckpointStore(Path(tmp) / ".cache-checkpoint.json")
            self.assertTrue(reloaded.completed("BTC", "1d"))


class CollectorTest(unittest.TestCase):
    def test_collect_coinbase_coin_writes_files_and_sidecar(self):
        import tempfile
        base_1m = minute_bars(datetime(2026, 11, 23, 23, 0, tzinfo=UTC), 120)
        base_1h = [bar((datetime(2026, 11, 17, tzinfo=UTC) + timedelta(hours=i)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ"), volume=5.0)
                   for i in range(168)]
        base_1d = [bar((datetime(2026, 8, 26, tzinfo=UTC) + timedelta(days=i)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ"), volume=7.0)
                   for i in range(90)]

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url):
                self.calls.append(url)
                query = parse_qs(urlparse(url).query)
                granularity = query["granularity"][0]
                rows_by_granularity = {"60": base_1m, "3600": base_1h,
                                       "86400": base_1d}
                rows = []
                for b in rows_by_granularity[granularity]:
                    epoch = int(datetime.fromisoformat(
                        b["t"].replace("Z", "+00:00")).timestamp())
                    rows.append([epoch, b["low"], b["high"], b["open"],
                                 b["close"], b.get("volume", 0.0)])
                rows.sort(reverse=True)
                return FakeResponse(200, rows[:300])

        clients = {"coinbase": CoinbaseClient(session=Session(), delay_seconds=0),
                   "tiingo": TiingoCryptoClient(token="x", session=FakeTiingoSession({}),
                                                delay_seconds=0),
                   "coingecko": CoinGeckoClient(session=FakeCoinGeckoSession({}),
                                                delay_seconds=0)}
        with tempfile.TemporaryDirectory() as tmp:
            summary = collector_mod.collect_coin(
                coins_mod.BY_SYMBOL["BTC"], clients, tmp, NOW,
                windows={"1m": timedelta(days=7), "1h": timedelta(days=90),
                         "1d": None})
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(len(summary["intervals"]), 11)
            stored = cache_mod.read_symbol_files(tmp, "BTC")
            self.assertEqual(len(stored["1m"]["bars"]), 120)
            self.assertEqual(len(stored["1h"]["bars"]), 168)
            self.assertEqual(len(stored["1d"]["bars"]), 90)
            self.assertEqual(len(stored["1w"]["bars"]), 14)
            self.assertEqual(stored["1m"]["bars"][0]["origin"], "recorded")
            self.assertEqual(stored["5m"]["bars"][0]["origin"], "reconstructed")
            sidecar = json.loads(
                (Path(tmp) / "crypto" / "candles" / "BTC" / "provenance.json"
                 ).read_text(encoding="utf-8"))
            self.assertEqual(sidecar["schema"], "snapshot_provenance.v1")
            self.assertEqual(sidecar["files"]["1d"]["source"], "coinbase")

    def test_collect_coingecko_coin_has_no_intraday(self):
        import tempfile
        rows = []
        for i in range(10):
            moment = datetime(2026, 11, 14, tzinfo=UTC) + timedelta(days=i)
            rows.append([int(moment.timestamp() * 1000), 1.0, 1.1, 0.9, 1.05])
        clients = {"coinbase": CoinbaseClient(session=FakeCoinbaseSession({}),
                                              delay_seconds=0),
                   "tiingo": TiingoCryptoClient(token="x", session=FakeTiingoSession({}),
                                                delay_seconds=0),
                   "coingecko": CoinGeckoClient(
                       session=FakeCoinGeckoSession({"tron": rows}), delay_seconds=0)}
        with tempfile.TemporaryDirectory() as tmp:
            summary = collector_mod.collect_coin(
                coins_mod.BY_SYMBOL["TRX"], clients, tmp, NOW)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["intervals"], ["1d", "1w"])
            self.assertEqual(sorted(summary["absent"]),
                             ["15m", "1h", "1m", "2h", "30m", "3m", "4h", "5m", "9m"])
            sidecar = json.loads(
                (Path(tmp) / "crypto" / "candles" / "TRX" / "provenance.json"
                 ).read_text(encoding="utf-8"))
            self.assertEqual(sidecar["files"]["1m"]["coverage"], "absent")
            self.assertIn("no 1m/1h series", sidecar["files"]["1m"]["reason"])

    def test_update_merges_new_bars(self):
        import tempfile
        base_1h = [bar((datetime(2026, 11, 23, 0, 0, tzinfo=UTC)
                        + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       volume=5.0) for i in range(10)]

        class Session:
            def __init__(self, rows):
                self.rows = rows

            def get(self, url):
                query = parse_qs(urlparse(url).query)
                granularity = query["granularity"][0]
                if granularity != "3600":
                    return FakeResponse(200, [])
                rows = []
                for b in self.rows:
                    epoch = int(datetime.fromisoformat(
                        b["t"].replace("Z", "+00:00")).timestamp())
                    rows.append([epoch, b["low"], b["high"], b["open"],
                                 b["close"], b.get("volume", 0.0)])
                rows.sort(reverse=True)
                return FakeResponse(200, rows[:300])

        clients = {"coinbase": CoinbaseClient(session=Session(base_1h),
                                              delay_seconds=0),
                   "tiingo": TiingoCryptoClient(token="x", session=FakeTiingoSession({}),
                                                delay_seconds=0),
                   "coingecko": CoinGeckoClient(session=FakeCoinGeckoSession({}),
                                                delay_seconds=0)}
        with tempfile.TemporaryDirectory() as tmp:
            # Seed stored files with the first 10 hourly bars only.
            stored_1h = cache_mod.build_interval_file(
                "BTC", "1h",
                cache_mod.envelope_bars(base_1h, FETCHED_AT, "recorded"),
                "coinbase", FETCHED_AT)
            cache_mod.write_interval_file(tmp, "BTC", "1h", stored_1h)
            stored_1d = cache_mod.build_interval_file(
                "BTC", "1d",
                cache_mod.envelope_bars(
                    [bar("2026-11-23T00:00:00Z", volume=7.0)], FETCHED_AT,
                    "recorded"),
                "coinbase", FETCHED_AT)
            cache_mod.write_interval_file(tmp, "BTC", "1d", stored_1d)
            stored_1m = cache_mod.build_interval_file(
                "BTC", "1m",
                cache_mod.envelope_bars(
                    minute_bars(datetime(2026, 11, 23, 23, 0, tzinfo=UTC), 5),
                    FETCHED_AT, "recorded"),
                "coinbase", FETCHED_AT)
            cache_mod.write_interval_file(tmp, "BTC", "1m", stored_1m)
            # New data: two more hourly bars arrive.
            extra = [bar("2026-11-23T10:00:00Z", volume=5.0),
                     bar("2026-11-23T11:00:00Z", volume=5.0)]
            clients["coinbase"].session.rows = base_1h + extra
            summary = collector_mod.update_coin(
                coins_mod.BY_SYMBOL["BTC"], clients, tmp, NOW)
            self.assertEqual(summary["status"], "ok")
            stored = cache_mod.read_symbol_files(tmp, "BTC")
            self.assertEqual(len(stored["1h"]["bars"]), 12)


class SeedTest(unittest.TestCase):
    """The checked-in BTC seed: real Coinbase bars, gap-free, enveloped."""

    SEED = ROOT / "market_data" / "crypto_candle_cache" / "seed" / "BTC"

    def test_seed_files_exist_and_parse(self):
        for name in ("1m.json", "1h.json", "1d.json", "provenance.json"):
            self.assertTrue((self.SEED / name).exists(), name)
        files = {name: cache_mod.read_interval_file(self.SEED / f"{name}.json")
                 for name in ("1m", "1h", "1d")}
        self.assertEqual(files["1m"]["symbol"], "BTC")
        self.assertEqual(files["1h"]["source"], "coinbase")
        total = sum(len(c["bars"]) for c in files.values())
        self.assertGreaterEqual(total, 300)
        self.assertLessEqual(total, 500)

    def test_seed_bars_are_gap_free_and_enveloped(self):
        for interval in ("1m", "1h", "1d"):
            content = cache_mod.read_interval_file(self.SEED / f"{interval}.json")
            self.assertEqual(oc.detect_gaps(content["bars"], interval), [],
                             interval)
            for b in content["bars"]:
                self.assertEqual(b["origin"], "recorded")
                self.assertIn("observed_at", b)
                self.assertIn("volume", b)
                self.assertNotIn("forming", b)

    def test_seed_selection_serves_as_of(self):
        content = cache_mod.read_interval_file(self.SEED / "1h.json")
        rows, forming = oc.to_observations(
            content["bars"], "BTC", "1h", "crypto/candles/BTC/1h.json",
            now_ms=oc.instant_ms(content["generated_at"]),
            provider=content["source"])
        self.assertEqual(forming, [])
        reading = oc.reading_as_of(
            rows, {"session": "continuous", "instrument": "BTC",
                   "cutoff": content["generated_at"]}, max_age_ms=2 * 3600 * 1000)
        self.assertEqual(reading["state"], "fresh")


if __name__ == "__main__":
    unittest.main()
