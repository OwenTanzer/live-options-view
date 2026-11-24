from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import unittest

from market_data.panel_prices import Capture, collect, crypto_time, mark


NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


class Fixture(Capture):
    def __init__(self, handler):
        super().__init__()
        self.handler = handler

    def get(self, provider, url, params=None):
        with self.lock:
            self.calls[provider] += 1
        return self.handler(url, params), NOW


class PriceCaptureTests(unittest.TestCase):
    def test_crypto_sources_and_enrichment_remain_independent(self):
        baseline = {"generated_at": "yesterday", "coins": [
            {"symbol": "BTC", "source": "coinbase", "price": 40, "volume_24h": 7},
            {"symbol": "USDT", "source": "coingecko"},
            {"symbol": "NEW", "source": "coingecko"},
            {"symbol": "NO", "source": "unavailable"}],
            "universe": {"added": [{"symbol": "NEW", "coingecko_id": "new-coin"}]}}
        original = deepcopy(baseline)

        def handler(url, params):
            if url.endswith("BTC-USD/ticker"):
                return {"price": "41", "time": "2026-11-24T14:59:12Z"}
            self.assertTrue(url.endswith("/simple/price"))
            self.assertEqual(set(params["ids"].split(",")), {"tether", "new-coin"})
            return {"tether": {"usd": 1.0, "last_updated_at": NOW.timestamp() - 61}}

        result = collect("crypto", baseline, Fixture(handler))
        self.assertEqual(result["enrichment"], original)
        self.assertEqual(result["calls"], {"coinbase": 1, "coingecko": 1})
        self.assertEqual(result["coverage"], {"fresh": 1, "stale": 1, "missing": 2})
        self.assertEqual(result["prices"]["BTC"]["price_usd"], 41)
        self.assertIsNone(result["prices"]["NO"]["received_at"])

    def test_nasdaq_uses_primary_on_ambiguous_changed_closes_and_partial_results(self):
        baseline = {"stocks": [{"symbol": s, "price": 10} for s in ("A", "B", "C")]}
        def handler(url, params):
            if url.endswith("/watchlist"):
                return {"data": [
                    {"symbol": "A", "lastSalePrice": "$10", "lastTradeTimestamp": "Nov 20, 2026"},
                    {"symbol": "B", "lastSalePrice": "$9", "lastTradeTimestamp": "Nov 20, 2026"}]}
            if url.endswith("/B/info"):
                return {"data": {"symbol": "B", "primaryData": {
                    "lastSalePrice": "$11", "lastTradeTimestamp": "Nov 24, 2026 10:00 AM ET"}}}
            return {}
        result = collect("nasdaq", baseline, Fixture(handler))
        self.assertEqual(result["calls"], {"nasdaq": 3})
        self.assertEqual(result["coverage"], {"fresh": 1, "stale": 1, "missing": 1})
        self.assertIsNone(result["prices"]["A"]["provider_observed_at"])
        self.assertEqual(result["prices"]["B"]["price_usd"], 11)
        self.assertEqual(result["prices"]["B"]["source"], "nasdaq_primary")
        self.assertIsNone(result["prices"]["C"]["price_usd"])

    def test_clock_and_numeric_validation(self):
        for price in [None, 0, "NaN", "Infinity", -3]:
            self.assertEqual(mark("test", price)["freshness"], "missing")
        self.assertIsNone(crypto_time("2026-11-24T10:00:00"))
        self.assertIsNone(crypto_time(float("inf")))
        self.assertEqual(mark("test", 1, None, NOW, "Nov 24, 2026")["freshness"], "unknown_time")

    @patch("market_data.panel_prices.requests.get")
    def test_throttle_and_deadline_skip_unissued_requests(self, get):
        network = Capture()
        get.return_value = Mock(status_code=429, headers={"Retry-After": "120"})
        network.get("provider", "url")
        network.get("provider", "url")
        network.deadline = 0
        network.get("another", "url")
        self.assertEqual(network.calls, {"provider": 1})
        self.assertEqual(network.backoff, {"provider": "120"})
        self.assertEqual(network.failures, {"provider.http_429": 1})
        get.assert_called_once()
