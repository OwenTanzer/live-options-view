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


class TestDynamicUniverse(unittest.TestCase):
    """The live top-50 resolver: tokenized exclusion, probing, fallback."""

    def _top_rows(self):
        # Live top 50 after tokenized exclusion, as verified live
        # 2026-11-24: the embedded 39 minus ASTER/DOT/ATOM, plus
        # USDD/BFUSD/HTX and eleven further entrants; two tokenized
        # rows sit inside the raw top 100 and must be excluded.
        kept = [c for c in coins_mod.COINS
                if c.symbol not in ("ASTER", "DOT", "ATOM")]
        rows = []
        rank = 1
        for coin in kept:
            rows.append({"id": coin.coingecko_id,
                         "symbol": coin.symbol.lower(),
                         "name": coin.name, "market_cap_rank": rank})
            rank += 1
        entrants = [
            ("usdd", "USDD", "USDD", 51),
            ("bfusd", "BFUSD", "BFUSD", 55),
            ("htx-dao", "HTX", "HTX DAO", 57),
            ("mantle", "MNT", "Mantle", 58),
            ("venice-token", "VVV", "Venice Token", 60),
            ("just", "JST", "JUST", 61),
            ("internet-computer", "ICP", "Internet Computer", 62),
            ("filecoin", "FIL", "Filecoin", 63),
            ("aptos", "APT", "Aptos", 64),
            ("arbitrum", "ARB", "Arbitrum", 65),
            ("optimism", "OP", "Optimism", 66),
            ("immutable", "IMX", "Immutable", 67),
            ("stacks", "STX", "Stacks", 68),
            ("the-graph", "GRT", "The Graph", 69),
        ]
        for coin_id, symbol, name, rank in entrants:
            rows.append({"id": coin_id, "symbol": symbol.lower(),
                         "name": name, "market_cap_rank": rank})
        rows.append({"id": "figure-heloc", "symbol": "figr_heloc",
                     "name": "Figure Heloc", "market_cap_rank": 9})
        rows.append({"id": "tether-gold", "symbol": "xaut",
                     "name": "Tether Gold", "market_cap_rank": 28})
        rows.sort(key=lambda r: r["market_cap_rank"])
        return rows

    def test_parse_top_markets_excludes_tokenized_and_takes_first_50(self):
        from market_data.crypto_collector.coins import parse_top_markets
        entries = parse_top_markets(self._top_rows())
        self.assertEqual(len(entries), 50)
        self.assertNotIn("figure-heloc", [e["coingecko_id"] for e in entries])
        self.assertNotIn("tether-gold", [e["coingecko_id"] for e in entries])
        self.assertEqual([e["rank"] for e in entries],
                         sorted(e["rank"] for e in entries))
        self.assertIn("usdd", [e["coingecko_id"] for e in entries])

    def test_parse_top_markets_rejects_partial_and_garbled(self):
        from market_data.crypto_collector.coins import parse_top_markets
        with self.assertRaises(ValueError):
            parse_top_markets(self._top_rows()[:10])
        with self.assertRaises(ValueError):
            parse_top_markets({"not": "a list"})
        with self.assertRaises(ValueError):
            parse_top_markets([])

    def _fake_session(self, rows):
        import requests

        class FakeResponse:
            def __init__(self, payload=None, status=200):
                self._payload = payload
                self.status_code = status

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code != 200:
                    response = requests.Response()
                    response.status_code = self.status_code
                    raise requests.HTTPError(
                        f"HTTP {self.status_code}", response=response)

        class FakeSession:
            def get(self, url, params=None, timeout=None):
                if "/coins/markets" in url:
                    return FakeResponse(rows)
                if "/products/MNT-USD/ticker" in url:
                    return FakeResponse({"price": "1.00",
                                         "time": "2026-11-23T21:55:00Z"})
                if "/products/" in url and "/ticker" in url:
                    return FakeResponse(None, status=404)
                raise AssertionError(f"unexpected URL {url}")

        return FakeSession()

    def test_resolve_live_top_50_with_probed_sources(self):
        from market_data.crypto_collector.collector import resolve_universe
        resolution = resolve_universe(self._fake_session(self._top_rows()),
                                      now=NOW)
        self.assertEqual(resolution["source"], "live")
        self.assertEqual(len(resolution["coins"]), 50)
        by_symbol = {c.symbol: c for c in resolution["coins"]}
        # Entrants with no Coinbase pair are CoinGecko-sourced;
        # the one entrant whose pair answers is Coinbase-sourced.
        for symbol in ("USDD", "BFUSD", "HTX"):
            self.assertEqual(by_symbol[symbol].source, "coingecko", symbol)
        self.assertEqual(by_symbol["MNT"].source, "coinbase")
        self.assertEqual(by_symbol["MNT"].coinbase_product, "MNT-USD")
        # Embedded coins keep their verified labels and live ranks.
        self.assertEqual(by_symbol["USDT"].source, "coingecko")
        self.assertEqual(by_symbol["BTC"].rank, 1)
        added = [r["symbol"] for r in resolution["added"]]
        removed = [r["symbol"] for r in resolution["removed"]]
        self.assertIn("USDD", added)
        self.assertIn("BFUSD", added)
        self.assertIn("HTX", added)
        self.assertEqual(sorted(removed), ["ASTER", "ATOM", "DOT"])
        usdd = next(r for r in resolution["added"] if r["symbol"] == "USDD")
        self.assertEqual(usdd["rank"], 51)
        self.assertEqual(usdd["source"], "coingecko")
        self.assertEqual(resolution["as_of"], "2026-11-23")

    def test_resolve_falls_back_on_failed_and_partial_list(self):
        import requests
        from market_data.crypto_collector.collector import resolve_universe

        class FailingSession:
            def get(self, url, params=None, timeout=None):
                raise requests.RequestException("boom")

        for session, rows in ((FailingSession(), None),
                              (self._fake_session(self._top_rows()[:10]), None)):
            resolution = resolve_universe(session, now=NOW)
            self.assertEqual(resolution["source"], "embedded_fallback")
            self.assertEqual(resolution["coins"], coins_mod.COINS)
            self.assertEqual(resolution["added"], [])
            self.assertEqual(resolution["removed"], [])
            self.assertIsNotNone(resolution["error"])

    def test_unavailable_entrant_probe_preserves_verified_universe(self):
        import requests
        from unittest.mock import patch
        from market_data.crypto_collector.collector import resolve_universe

        session = self._fake_session(self._top_rows())
        fetch = session.get
        with patch.object(session, "get") as get:
            def temporary_outage(url, **kwargs):
                if "/products/" in url:
                    raise requests.Timeout("probe timed out")
                return fetch(url, **kwargs)
            get.side_effect = temporary_outage
            result = resolve_universe(session, now=NOW)
        self.assertEqual(result["coins"], coins_mod.COINS)
        self.assertEqual(result["source"], "embedded_fallback")
        self.assertIn("probe timed out", result["error"])

    def test_snapshot_from_live_universe_has_50_rows_and_changes(self):
        from market_data.crypto_collector.collector import (
            build_snapshot, resolve_universe)
        resolution = resolve_universe(self._fake_session(self._top_rows()),
                                      now=NOW)
        coinbase, cg = {}, {}
        for coin in resolution["coins"]:
            cg[coin.coingecko_id] = _cg_row(coin)
            if coin.source == "coinbase":
                coinbase[coin.symbol] = _coinbase_entry()
        snap = build_snapshot(
            coinbase=coinbase, coingecko_markets=cg,
            coingecko_global={"total_market_cap": {"usd": 2_130_000_000_000.0},
                              "market_cap_percentage": {"btc": 58.0}},
            fear_greed={"value": 11, "classification": "Extreme Fear",
                        "as_of": "2026-11-23T21:00:00Z", "status": "ok",
                        "source": "alternative.me"},
            now=NOW, universe=resolution)
        self.assertEqual(len(snap["coins"]), 50)
        self.assertEqual(snap["counts"]["coins"], 50)
        self.assertEqual(snap["universe"]["source"], "live")
        self.assertEqual(snap["universe"]["coins"], 50)
        self.assertEqual(
            [r["symbol"] for r in snap["universe"]["added"]][:3],
            ["USDD", "BFUSD", "HTX"])
        self.assertEqual(
            sorted(r["symbol"] for r in snap["universe"]["removed"]),
            ["ASTER", "ATOM", "DOT"])
        sidecar = build_provenance(snap, publication_status="published")
        self.assertEqual(sidecar["universe"]["source"], "live")
        self.assertEqual(len(sidecar["universe"]["added"]), 14)


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


class PublicationTests(unittest.TestCase):
    def test_partial_publication_keeps_matching_provenance_in_latest(self):
        import hashlib
        import json
        from unittest.mock import Mock, patch
        from market_data.crypto_collector.publish import _publish_snapshot, SIDECAR_KEY, LATEST_KEY

        objects = {}
        def write(**kw):
            objects[kw["Key"]] = kw["Body"]
        client = Mock()
        client.put_object.side_effect = write
        with patch("market_data.crypto_collector.publish.utcnow", return_value=NOW):
            receipt = _publish_snapshot({"constructed_at": "previous"}, client, "test")
        assert receipt["latest_published_at"] != "previous"
        assert receipt["snapshot_sha256"] == hashlib.sha256(objects[LATEST_KEY]).hexdigest()

        def fail_receipt(**kw):
            if kw["Key"] == SIDECAR_KEY:
                raise OSError("sidecar failed")
            write(**kw)
        client.put_object.side_effect = fail_receipt
        with self.assertRaisesRegex(OSError, "sidecar failed"):
            _publish_snapshot({"constructed_at": "new"}, client, "test")
        assert json.loads(objects[LATEST_KEY])["provenance"]["constructed_at"] == "new"
        assert json.loads(objects[SIDECAR_KEY])["snapshot_sha256"] != hashlib.sha256(objects[LATEST_KEY]).hexdigest()


if __name__ == "__main__":
    unittest.main()
