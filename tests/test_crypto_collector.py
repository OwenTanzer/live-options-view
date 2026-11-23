"""Offline tests for the crypto collector.

Run: ``python -m unittest tests.test_crypto_collector -v`` from the
repository root, or ``python tests/test_crypto_collector.py``.

Every test here is offline: provider payloads are fixtures, never
live calls. A live fetch is a separate verification step (see the
collector README), not part of this suite.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.crypto_collector import coins as coins_mod
from market_data.crypto_collector.collector import (
    build_snapshot,
    freshness_status,
)
from market_data.crypto_collector.patterns import (
    dedupe_patterns,
    detect_patterns,
)
from market_data.crypto_collector.provenance import build_provenance

NOW = datetime(2026, 11, 23, 22, 0, 0, tzinfo=timezone.utc)


def _cg_row(coin, *, price=100.0, change_24h=1.0, change_1h=0.5,
            change_7d=2.0, market_cap=1_000_000_000.0):
    return {
        "id": coin.coingecko_id,
        "symbol": coin.symbol.lower(),
        "current_price": price,
        "market_cap": market_cap,
        "market_cap_rank": coin.rank,
        "high_24h": price * 1.05,
        "low_24h": price * 0.95,
        "price_change_percentage_24h": change_24h,
        "price_change_percentage_1h_in_currency": change_1h,
        "price_change_percentage_7d_in_currency": change_7d,
        "total_volume": 10_000_000.0,
        "last_updated": "2026-11-23T21:55:00Z",
    }


def _coinbase_entry(*, price=100.0, open_24h=99.0):
    return {
        "ticker": {"price": str(price), "time": "2026-11-23T21:55:00Z"},
        "stats": {"open": str(open_24h), "high": str(price * 1.05),
                  "low": str(price * 0.95), "volume": "1234.5"},
    }


def _fixture_inputs(*, coinbase_error_symbols=(), missing_cg_symbols=()):
    coinbase = {}
    cg = {}
    for coin in coins_mod.COINS:
        if coin.source == "coinbase":
            if coin.symbol in coinbase_error_symbols:
                coinbase[coin.symbol] = {
                    "ticker": {"error": "HTTP 404"},
                    "stats": {"error": "HTTP 404"},
                }
            else:
                coinbase[coin.symbol] = _coinbase_entry()
        if coin.symbol not in missing_cg_symbols:
            cg[coin.coingecko_id] = _cg_row(coin)
    return {
        "coinbase": coinbase,
        "coingecko_markets": cg,
        "coingecko_global": {
            "total_market_cap": {"usd": 2_130_000_000_000.0},
            "market_cap_percentage": {"btc": 58.0},
        },
        "fear_greed": {"value": 11, "classification": "Extreme Fear",
                       "as_of": "2026-11-23T21:00:00Z", "status": "ok",
                       "source": "alternative.me"},
    }


class TestUniverse(unittest.TestCase):
    def test_39_coins_30_coinbase_9_coingecko(self):
        self.assertEqual(len(coins_mod.COINS), 39)
        self.assertEqual(len(coins_mod.COINBASE_COINS), 30)
        self.assertEqual(len(coins_mod.COINGECKO_COINS), 9)

    def test_coingecko_nine_are_the_tracker_nine(self):
        self.assertEqual(
            [c.symbol for c in coins_mod.COINGECKO_COINS],
            ["USDT", "USDC", "TRX", "XMR", "LEO", "WBT", "DAI", "CC", "GRAM"])

    def test_rank_order_is_strictly_increasing(self):
        ranks = [c.rank for c in coins_mod.COINS]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), 39)


class TestSnapshotBuild(unittest.TestCase):
    def test_source_labelling_per_coin(self):
        snap = build_snapshot(**_fixture_inputs(), now=NOW)
        by_symbol = {r["symbol"]: r for r in snap["coins"]}
        for coin in coins_mod.COINS:
            row = by_symbol[coin.symbol]
            self.assertEqual(row["source"], coin.source, coin.symbol)
            self.assertEqual(row["price_basis"], coin.source, coin.symbol)
            self.assertEqual(row["price"], 100.0, coin.symbol)

    def test_rank_order_in_snapshot(self):
        snap = build_snapshot(**_fixture_inputs(), now=NOW)
        ranks = [r["rank"] for r in snap["coins"]]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(snap["coins"]), 39)

    def test_as_of_and_freshness_fields(self):
        snap = build_snapshot(**_fixture_inputs(), now=NOW)
        self.assertEqual(snap["constructed_at"], "2026-11-23T22:00:00Z")
        self.assertEqual(snap["session_model"], "continuous_24_7")
        self.assertEqual(snap["cadence"], "hourly")
        for row in snap["coins"]:
            self.assertEqual(row["as_of"], "2026-11-23T21:55:00Z")
            self.assertEqual(row["provider_observed_at"], "2026-11-23T21:55:00Z")
            self.assertEqual(row["freshness"], "fresh")
            self.assertEqual(row["status"], "ok")
        # Coinbase stats volume is base units; volume_24h is USD.
        btc = next(r for r in snap["coins"] if r["symbol"] == "BTC")
        self.assertEqual(btc["volume_24h"], 1234.5 * 100.0)
        self.assertEqual(btc["volume_24h_unit"], "USD")

    def test_freshness_boundaries(self):
        self.assertEqual(
            freshness_status("2026-11-23T21:55:00Z", now=NOW), "fresh")
        self.assertEqual(
            freshness_status("2026-11-23T19:59:00Z", now=NOW), "stale")
        self.assertEqual(freshness_status(None, now=NOW), "unavailable")
        self.assertEqual(
            freshness_status("2026-11-23T23:00:00Z", now=NOW), "stale")

    def test_coinbase_404_is_source_unavailable_not_coingecko_price(self):
        # A Coinbase fetch failure (simulated here for ASTER) must
        # leave the row empty even though CoinGecko metadata for
        # ASTER was fetched.
        snap = build_snapshot(
            **_fixture_inputs(coinbase_error_symbols={"ASTER"}), now=NOW)
        aster = next(r for r in snap["coins"] if r["symbol"] == "ASTER")
        self.assertIsNone(aster["price"])
        self.assertEqual(aster["status"], "source_unavailable")
        self.assertEqual(aster["price_basis"], "coinbase")
        self.assertEqual(aster["freshness"], "unavailable")
        self.assertIn("404", aster["status_reason"])
        self.assertEqual(snap["counts"]["source_unavailable"], 1)
        # Every other coin is untouched.
        btc = next(r for r in snap["coins"] if r["symbol"] == "BTC")
        self.assertEqual(btc["price"], 100.0)

    def test_coingecko_outage_marks_only_coingecko_coins(self):
        snap = build_snapshot(
            coinbase=_fixture_inputs()["coinbase"],
            coingecko_markets={"_error": "HTTP 429"},
            coingecko_global={"error": "HTTP 429"},
            fear_greed={"value": None, "classification": None, "as_of": None,
                        "status": "unavailable", "reason": "HTTP 429",
                        "source": "alternative.me"},
            now=NOW)
        by_symbol = {r["symbol"]: r for r in snap["coins"]}
        for coin in coins_mod.COINGECKO_COINS:
            row = by_symbol[coin.symbol]
            self.assertIsNone(row["price"], coin.symbol)
            self.assertEqual(row["status"], "source_unavailable", coin.symbol)
            self.assertEqual(row["price_basis"], "coingecko", coin.symbol)
        btc = by_symbol["BTC"]
        self.assertEqual(btc["price"], 100.0)
        # Rank falls back to the fixed verification rank; market cap
        # metadata is absent, not invented.
        self.assertEqual(btc["rank"], 1)
        self.assertIsNone(btc["market_cap"])
        self.assertEqual(snap["market"]["fear_greed"]["status"], "unavailable")
        self.assertIsNone(snap["market"]["fear_greed"]["value"])

    def test_fear_greed_unavailable_state_is_explicit(self):
        snap = build_snapshot(**_fixture_inputs(), now=NOW)
        self.assertEqual(snap["market"]["fear_greed"]["value"], 11)
        self.assertEqual(snap["market"]["fear_greed"]["as_of"],
                         "2026-11-23T21:00:00Z")


class TestProvenance(unittest.TestCase):
    def test_sidecar_fields(self):
        snap = build_snapshot(**_fixture_inputs(), now=NOW)
        sidecar = build_provenance(
            snap, publication_status="published",
            latest_published_at="2026-11-23T22:00:00Z")
        self.assertEqual(sidecar["schema_version"], "snapshot_provenance.v1")
        self.assertEqual(sidecar["artifact"], "crypto/latest.json")
        self.assertEqual(sidecar["constructed_at"], "2026-11-23T22:00:00Z")
        self.assertIsNone(sidecar["archive_uploaded_at"])
        self.assertEqual(sidecar["latest_published_at"], "2026-11-23T22:00:00Z")
        self.assertEqual(sidecar["publication"]["status"], "published")
        btc = sidecar["coins"]["BTC"]
        self.assertEqual(btc["price_basis"], "coinbase")
        self.assertEqual(btc["provider_observed_at"], "2026-11-23T21:55:00Z")
        self.assertEqual(btc["freshness"], "fresh")


class TestPatterns(unittest.TestCase):
    def _rows(self, **overrides):
        rows = []
        for coin in coins_mod.COINS:
            row = {
                "symbol": coin.symbol, "status": "ok", "price": 100.0,
                "change_24h_pct": 1.0, "change_1h_pct": 0.5,
                "change_7d_pct": 2.0, "high_24h": 105.0, "low_24h": 95.0,
                "volume_24h": 10_000_000.0, "market_cap": 1_000_000_000.0,
            }
            row.update(overrides.get(coin.symbol, {}))
            rows.append(row)
        return rows

    def test_sharp_move_and_severity(self):
        rows = self._rows(BTC={"change_24h_pct": 12.0},
                          ETH={"change_24h_pct": -25.0})
        found = detect_patterns(rows, {}, None, now=NOW)
        kinds = {(p["kind"], p["coins"][0]): p["severity"] for p in found
                 if p["kind"] == "sharp_24h_move"}
        self.assertEqual(kinds[("sharp_24h_move", "BTC")], "notable")
        self.assertEqual(kinds[("sharp_24h_move", "ETH")], "extreme")

    def test_fear_greed_extreme(self):
        found = detect_patterns(
            self._rows(), {"fear_greed": {"value": 11, "classification": "Extreme Fear"}},
            None, now=NOW)
        self.assertTrue(any(p["kind"] == "fear_greed_extreme" and
                            p["severity"] == "extreme" for p in found))

    def test_broad_selloff_breadth(self):
        rows = self._rows()
        for row in rows:
            row["change_24h_pct"] = -3.0
        found = detect_patterns(rows, {}, None, now=NOW)
        self.assertTrue(any(p["kind"] == "broad_selloff" for p in found))
        self.assertFalse(any(p["kind"] == "broad_rally" for p in found))

    def test_dedupe_within_90_minutes(self):
        rows = self._rows(BTC={"change_24h_pct": 12.0})
        fresh = detect_patterns(rows, {}, None, now=NOW)
        sharp = [p for p in fresh if p["kind"] == "sharp_24h_move"]
        self.assertEqual(len(sharp), 1)
        # Same pattern 60 minutes later is suppressed.
        later = NOW + timedelta(minutes=60)
        again = detect_patterns(rows, {}, None, now=later)
        kept = dedupe_patterns(again, fresh, now=later)
        self.assertFalse(any(p["kind"] == "sharp_24h_move" for p in kept))
        # ... but 120 minutes later it re-appears.
        much_later = NOW + timedelta(minutes=120)
        again2 = detect_patterns(rows, {}, None, now=much_later)
        kept2 = dedupe_patterns(again2, fresh, now=much_later)
        self.assertTrue(any(p["kind"] == "sharp_24h_move" for p in kept2))

    def test_jump_since_last_update_needs_previous(self):
        rows = self._rows(BTC={"price": 106.0})
        self.assertFalse(any(p["kind"] == "jump_since_last_update"
                             for p in detect_patterns(rows, {}, None, now=NOW)))
        previous = {"coins": [{"symbol": "BTC", "price": 100.0}],
                    "market": {}}
        found = detect_patterns(rows, {}, previous, now=NOW)
        self.assertTrue(any(p["kind"] == "jump_since_last_update" and
                            p["coins"] == ["BTC"] for p in found))


if __name__ == "__main__":
    unittest.main()
