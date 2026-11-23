"""Regression: every servable ticker has a class; a priced ticker never 502s.

NVDA returned HTTP 502 because TICKER_CLASSES (hand-written, 17 entries)
did not cover 82 of the 99 PRICE_TICKERS labels, and quote_payload indexed
it with TICKER_CLASSES[symbol]. TICKER_CLASSES is now derived from the
canonical exact-100 universe, and the handler fails closed with
ticker_unavailable if a class is ever missing.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.live_market import quotes
from market_data.live_market.config import PRICE_TICKERS, TICKER_CLASSES

PINNED_CLASSES = {
    "QQQ": "equity", "SPY": "equity", "USO": "equity", "SMH": "equity",
    "IGV": "equity", "META": "equity", "GOOGL": "equity", "AMZN": "equity",
    "TSLA": "equity", "MU": "equity", "SPCX": "equity", "AAPL": "equity",
    "VIX": "index", "OVX": "index", "10Y": "yield", "JPY/USD": "futures",
    "KOSPI": "international", "BTC/USD": "crypto",
}

EXPECTED_CLASSES = {
    "NVDA": "equity", "MSFT": "equity", "TLT": "equity", "GLD": "equity",
    "BNO": "equity", "GVZ": "index", "FTSE100": "international",
    "DAX": "international", "FVX5Y": "yield", "UST2Y": "yield",
    "ETH/USD": "crypto", "SOL/USD": "crypto", "EUR/USD": "international",
    "/CL": "futures", "/GC": "futures",
}


class FakeFeed:
    def __init__(self, state):
        self.state = state

    def get_state(self):
        return dict(self.state)

    def get_health(self):
        return {"connected": True, "authorized": True, "channel_open": True,
                "last_feed_event_time": datetime.now(timezone.utc),
                "event_counts": {}, "quote_time_counts": {}}


def _price_row(price, ts="2026-11-10T14:30:00+00:00"):
    return {"bid": price - 0.05, "ask": price + 0.05, "last": price,
            "prev_close": price, "last_ts": ts, "bid_ts": ts, "ask_ts": ts}


class TestTickerClasses(unittest.TestCase):
    def test_every_servable_ticker_has_a_class(self):
        missing = [s for s in list(PRICE_TICKERS) + ["SPY"] if s not in TICKER_CLASSES]
        self.assertEqual(missing, [], "every PRICE_TICKERS label and SPY needs a class")

    def test_pinned_classes_unchanged(self):
        for symbol, expected in PINNED_CLASSES.items():
            self.assertEqual(TICKER_CLASSES[symbol], expected, symbol)

    def test_derived_classes_match_canonical_types(self):
        for symbol, expected in EXPECTED_CLASSES.items():
            self.assertEqual(TICKER_CLASSES[symbol], expected, symbol)

    def test_priced_tickers_return_quotes_with_classes(self):
        symbols = ["NVDA", "QQQ", "SPY", "TLT", "GLD", "BTC/USD",
                   "ZZZZ", "QQQ241101C00677000"]
        state = {}
        for symbol in ["NVDA", "QQQ", "SPY", "TLT", "GLD", "BTC/USD"]:
            dx = PRICE_TICKERS.get(symbol, symbol)
            state[dx] = _price_row(100.0)
        registry = quotes.LiveQuoteRegistry()
        registry.set_session(FakeFeed(state), {})
        payload = registry.quote_payload(symbols)
        by_symbol = {q["symbol"]: q for q in payload["quotes"]}
        for symbol in ["NVDA", "QQQ", "SPY", "TLT", "GLD", "BTC/USD"]:
            self.assertIn(symbol, by_symbol, symbol)
            self.assertEqual(by_symbol[symbol]["instrument_class"],
                             TICKER_CLASSES[symbol], symbol)
            self.assertEqual(by_symbol[symbol]["kind"], "ticker")
        self.assertEqual(payload["unavailable"].get("ZZZZ"), "unknown_symbol")
        self.assertEqual(payload["unavailable"].get("QQQ241101C00677000"), "expired")

    def test_fallback_price_uses_class(self):
        registry = quotes.LiveQuoteRegistry()
        registry.update_ticker_fallbacks(
            {"NVDA": {"price": 123.45, "quote_ts": "2026-11-10T14:30:00+00:00"},
             "VIX": {"price": 19.46, "quote_ts": "2026-11-10T14:30:00+00:00"}},
            "2026-11-10T14:30:00+00:00")
        payload = registry.quote_payload(["NVDA", "VIX"])
        by_symbol = {q["symbol"]: q for q in payload["quotes"]}
        self.assertEqual(by_symbol["NVDA"]["instrument_class"], "equity")
        self.assertEqual(by_symbol["VIX"]["instrument_class"], "index")

    def test_missing_class_fails_closed_not_502(self):
        state = {"NVDA": _price_row(100.0), "QQQ": _price_row(100.0)}
        registry = quotes.LiveQuoteRegistry()
        registry.set_session(FakeFeed(state), {})
        patched = {k: v for k, v in TICKER_CLASSES.items() if k != "NVDA"}
        with patch.object(quotes, "TICKER_CLASSES", patched):
            payload = registry.quote_payload(["NVDA", "QQQ"])
        self.assertEqual(payload["unavailable"].get("NVDA"), "ticker_unavailable")
        self.assertEqual([q["symbol"] for q in payload["quotes"]], ["QQQ"])

    def test_unpriced_ticker_is_unavailable_not_fake(self):
        registry = quotes.LiveQuoteRegistry()
        registry.set_session(FakeFeed({}), {})
        payload = registry.quote_payload(["NVDA"])
        self.assertEqual(payload["quotes"], [])
        self.assertEqual(payload["unavailable"], {"NVDA": "ticker_unavailable"})


if __name__ == "__main__":
    unittest.main()
