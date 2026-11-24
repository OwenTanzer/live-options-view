"""Unit tests for market_data/candle_cache_backend.

Covers: envelope stamping (One Clock field names and rules),
cache reader/writer round-trips, resample-from-base consistency,
the as-of selector (including gap labelling, rewind, corrections,
conflicts and stale-cache behaviour), the store abstraction, and
the seeded AAPL sample. Run from the repository root:

    python -m unittest tests.test_candle_cache_backend -v

The selector cases mirror the One Clock contract fixture
(tests/fixtures/one_clock_contract.json): before/at/after
publication, unknown availability, duplicate delivery, correction,
rewind, missing/partial data, session/instrument isolation.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.candle_cache_backend import cache as cache_mod
from market_data.candle_cache_backend import envelope as env
from market_data.candle_cache_backend import resample as resample_mod
from market_data.candle_cache_backend import selector as sel
from market_data.candle_cache_backend import store as store_mod
from market_data.candle_cache_backend.cli import SEED_PATH, cmd_seed

# One Clock contract fixture base row, verbatim from
# tests/fixtures/one_clock_contract.json (eventAt 1794234600000 is
# 2026-11-09T14:30:00Z); _rows() below re-bases it onto AAPL/2026-11-23.
FIXTURE_BASE = {
    "source": "fixture", "id": "signal", "revision": 1,
    "session": "2026-11-09", "instrument": "QQQ", "origin": "recorded",
    "evidence": "fixture://one-clock",
    "eventAt": 1794234600000, "availableAt": 1794234600000, "value": 1,
}


def bar(t, close, **extra):
    row = {"t": t, "open": close, "high": close + 1,
           "low": close - 1, "close": close}
    row.update(extra)
    return row


def minute_bars(day="2026-11-23", count=10, start_minute=870):
    """Synthetic 1m bars for selector tests (synthetic cohort).

    ``start_minute`` is minutes after UTC midnight; the default 870
    is 14:30Z = 09:30 ET, the regular-session open in November (EST).
    """
    bars = []
    for i in range(count):
        total = start_minute + i
        bars.append(bar(f"{day}T{total // 60:02d}:{total % 60:02d}:00Z",
                        100.0 + i))
    return bars


class EnvelopeTest(unittest.TestCase):
    def test_stamp_carries_every_one_clock_field(self):
        row = env.stamp_bar(bar("2026-11-23T14:30:00Z", 100.0),
                            symbol="AAPL", interval="1m",
                            available_at="2026-11-24T00:12:32Z")
        for field in ("source", "id", "revision", "session", "instrument",
                      "origin", "evidence", "eventAt", "availableAt", "value"):
            self.assertIn(field, row)
        self.assertEqual(row["source"], "candle-cache")
        self.assertEqual(row["id"], "AAPL:1m:2026-11-23T14:30:00Z")
        self.assertEqual(row["session"], "2026-11-23")  # ET date of the bar
        self.assertEqual(row["instrument"], "AAPL")
        self.assertEqual(row["origin"], "recorded")
        self.assertEqual(row["eventAt"], 1795444200000)
        self.assertEqual(row["availableAt"], 1795479152000)
        self.assertEqual(row["value"]["close"], 100.0)
        self.assertNotIn("volume", row["value"])  # intraday: no volume key

    def test_daily_bar_keeps_volume(self):
        row = env.stamp_bar(
            bar("2026-11-23T00:00:00Z", 349.08, volume=56934410,
                adj_close=349.08, adj_volume=56934410),
            symbol="AAPL", interval="1d",
            available_at="2026-11-24T00:12:32Z")
        self.assertEqual(row["value"]["volume"], 56934410)
        self.assertEqual(row["value"]["adj_close"], 349.08)

    def test_unknown_availability_is_null_never_substituted(self):
        row = env.stamp_bar(bar("2026-11-23T14:30:00Z", 100.0),
                            symbol="AAPL", interval="1m", available_at=None)
        self.assertIsNone(row["availableAt"])

    def test_availability_before_event_is_rejected(self):
        with self.assertRaises(ValueError):
            env.stamp_bar(bar("2026-11-23T14:30:00Z", 100.0),
                          symbol="AAPL", interval="1m",
                          available_at="2026-11-23T14:29:00Z")

    def test_bad_origin_and_revision_rejected(self):
        with self.assertRaises(ValueError):
            env.stamp_bar(bar("2026-11-23T14:30:00Z", 100.0),
                          symbol="AAPL", interval="1m", origin="guess")
        with self.assertRaises(ValueError):
            env.stamp_bar(bar("2026-11-23T14:30:00Z", 100.0),
                          symbol="AAPL", interval="1m", revision=0)

    def test_naive_time_and_epoch_seconds_rejected(self):
        with self.assertRaises(ValueError):
            env.parse_time("2026-11-23 14:30:00")
        with self.assertRaises(ValueError):
            env.parse_time(1794234600)  # seconds, not ms

    def test_chips_are_the_three_origins_only(self):
        for origin in ("recorded", "reconstructed", "synthetic"):
            self.assertEqual(env.chip_for(origin), origin)
        with self.assertRaises(ValueError):
            env.chip_for("live")


class SelectorTest(unittest.TestCase):
    def _rows(self, **overrides):
        base = dict(FIXTURE_BASE, source="candle-cache",
                    id="AAPL:1m:2026-11-23T14:30:00Z",
                    session="2026-11-23", instrument="AAPL",
                    evidence="nasdaq/candles/AAPL/1m.json",
                    eventAt=1795444200000, availableAt=1795444200000)
        base.update(overrides)
        return [base]

    def test_before_publication_is_unavailable(self):
        rows = self._rows()
        self.assertEqual(sel.select_as_of(
            rows, session="2026-11-23", instrument="AAPL",
            cutoff=1795444199999), [])
        reading = sel.reading_as_of(
            rows, session="2026-11-23", instrument="AAPL",
            cutoff=1795444199999, max_age_ms=60_000)
        self.assertEqual(reading["state"], "unavailable")
        self.assertIsNone(reading["ageMs"])

    def test_at_publication_is_fresh(self):
        rows = self._rows()
        reading = sel.reading_as_of(
            rows, session="2026-11-23", instrument="AAPL",
            cutoff=1795444200000, max_age_ms=60_000)
        self.assertEqual(reading["state"], "fresh")
        self.assertEqual(reading["ageMs"], 0)
        self.assertEqual(reading["publicationAgeMs"], 0)

    def test_after_max_age_is_stale_not_missing(self):
        rows = self._rows()
        reading = sel.reading_as_of(
            rows, session="2026-11-23", instrument="AAPL",
            cutoff=1795444200000 + 120_000, max_age_ms=60_000)
        self.assertEqual(reading["state"], "stale")
        self.assertEqual(reading["ageMs"], 120_000)
        self.assertIsNotNone(reading["row"])

    def test_unknown_availability_never_eligible(self):
        rows = self._rows(availableAt=None)
        self.assertEqual(sel.select_as_of(
            rows, session="2026-11-23", instrument="AAPL",
            cutoff=1795444200000 + 10 ** 9), [])

    def test_correction_wins_and_rewind_restores(self):
        old = self._rows()[0]
        new = dict(old, revision=2, availableAt=1795444200020, value=2)
        rows = [old, new]
        selected = sel.select_as_of(rows, session="2026-11-23",
                                    instrument="AAPL", cutoff=1795444200020)
        self.assertEqual(selected[0]["value"], 2)
        rewound = sel.select_as_of(rows, session="2026-11-23",
                                   instrument="AAPL", cutoff=1795444200010)
        self.assertEqual(rewound[0]["value"], 1)

    def test_conflicting_same_revision_raises(self):
        old = self._rows()[0]
        clash = dict(old, value=99)
        with self.assertRaises(ValueError):
            sel.select_as_of([old, clash], session="2026-11-23",
                             instrument="AAPL", cutoff=1795444200000)

    def test_duplicate_delivery_is_harmless(self):
        rows = self._rows() * 2
        selected = sel.select_as_of(rows, session="2026-11-23",
                                    instrument="AAPL", cutoff=1795444200000)
        self.assertEqual(len(selected), 1)

    def test_other_session_instrument_origin_are_isolated(self):
        rows = self._rows()
        for kwargs in ({"session": "2026-11-24"},
                       {"instrument": "MSFT"},
                       {"origin": "synthetic"}):
            self.assertEqual(sel.select_as_of(
                rows, session=kwargs.get("session", "2026-11-23"),
                instrument=kwargs.get("instrument", "AAPL"),
                origin=kwargs.get("origin", "recorded"),
                cutoff=1795444200000), [])

    def test_retraction_is_unavailable_not_resurrected(self):
        old = self._rows()[0]
        retracted = dict(old, revision=2, availableAt=1795444200020,
                         value=None)
        reading = sel.reading_as_of(
            [old, retracted], session="2026-11-23", instrument="AAPL",
            cutoff=1795444200020, max_age_ms=60_000)
        self.assertEqual(reading["state"], "unavailable")

    def test_max_age_must_be_explicit(self):
        with self.assertRaises(ValueError):
            sel.reading_as_of(self._rows(), session="2026-11-23",
                              instrument="AAPL", cutoff=1795444200000,
                              max_age_ms=None)


class GapTest(unittest.TestCase):
    def _observations(self, bars, **kwargs):
        return env.observations_for_bars(
            bars, symbol="AAPL", interval="1m", origin="synthetic",
            available_at="2026-11-24T00:00:00Z", **kwargs)

    def test_gap_inside_coverage_labelled_honestly(self):
        bars = minute_bars(count=390)  # full 09:30-16:00 ET session
        del bars[200]  # one missing minute inside the range
        rows = self._observations(bars)
        report = sel.gap_report(rows, session="2026-11-23")
        labels = [entry["label"] for entry in report]
        self.assertIn("reaction unmeasurable \u00b7 price gap", labels)
        self.assertNotIn("coverage partial", labels)

    def test_no_gap_no_label(self):
        rows = self._observations(minute_bars(count=390))
        report = sel.gap_report(rows, session="2026-11-23")
        self.assertEqual(report, [])

    def test_partial_session_labelled_coverage_partial(self):
        # Bars start at 10:00 ET (15:00Z), not the 09:30 open.
        bars = minute_bars(count=10, start_minute=900)
        rows = self._observations(bars)
        report = sel.gap_report(rows, session="2026-11-23")
        self.assertIn("coverage partial",
                      [entry["label"] for entry in report])

    def test_empty_selection_is_unavailable(self):
        report = sel.gap_report([], session="2026-11-23")
        self.assertEqual(report[0]["label"], "unavailable")

    def test_as_of_bars_carries_labels(self):
        bars = minute_bars(count=390)
        del bars[200]
        rows = self._observations(bars)
        result = sel.as_of_bars(rows, session="2026-11-23",
                                instrument="AAPL", origin="synthetic",
                                cutoff="2026-11-24T00:00:00Z",
                                max_age_ms=10 ** 12)
        self.assertEqual(len(result["bars"]), 389)
        self.assertIn("reaction unmeasurable \u00b7 price gap",
                      result["labels"])


class ResampleTest(unittest.TestCase):
    def test_derive_all_matches_base_counts(self):
        bars_1m = minute_bars(count=390)  # 09:30-16:00 ET session
        daily = [bar("2026-11-20T00:00:00Z", 340.0, volume=1000),
                 bar("2026-11-21T00:00:00Z", 342.0, volume=1100),
                 bar("2026-11-23T00:00:00Z", 349.0, volume=1200)]
        hourly = [bar("2026-11-23T15:00:00Z", 348.0),
                  bar("2026-11-23T16:00:00Z", 349.0),
                  bar("2026-11-23T17:00:00Z", 348.5)]
        derived = resample_mod.derive_all(
            {"1m": bars_1m, "1h": hourly, "1d": daily})
        self.assertEqual(set(derived), set(resample_mod.INTERVALS))
        # 390 session minutes -> 78 five-minute bars, 43 nine-minute bars.
        self.assertEqual(len(derived["5m"]), 78)
        self.assertEqual(len(derived["9m"]), 44)
        self.assertEqual(derived["5m"][0]["bars"], 5)
        # Two ISO weeks: 2026-11-20/21 (2100) and 2026-11-23 (1200).
        self.assertEqual(derived["1w"][0]["volume"], 2100)
        self.assertEqual(derived["1w"][1]["volume"], 1200)
        # Derived intraday bars carry no volume key.
        self.assertNotIn("volume", derived["5m"][0])
        # OHLC consistency: first 5m bar aggregates the first five 1m bars.
        self.assertEqual(derived["5m"][0]["open"], bars_1m[0]["open"])
        self.assertEqual(derived["5m"][0]["close"], bars_1m[4]["close"])
        self.assertEqual(derived["5m"][0]["high"],
                         max(b["high"] for b in bars_1m[:5]))

    def test_missing_base_raises(self):
        with self.assertRaises(ValueError):
            resample_mod.derive_all({"1m": [], "1h": []})


class StoreAndCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.LocalDirectoryStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_aapl(self):
        bases = {"1m": minute_bars(count=30),
                 "1h": [bar("2026-11-23T15:00:00Z", 348.0),
                        bar("2026-11-23T16:00:00Z", 349.0)],
                 "1d": [bar("2026-11-23T00:00:00Z", 349.08,
                            volume=56934410, adj_close=349.08,
                            adj_volume=56934410)]}
        derived = resample_mod.derive_all(bases)
        return cache_mod.write_symbol(
            self.store, "AAPL", derived,
            generated_at="2026-11-24T00:12:32Z",
            available_at="2026-11-24T00:12:32Z")

    def test_local_store_round_trip_and_atomic_write(self):
        self.store.write_json("nasdaq/candles/AAPL/1m.json", {"a": 1})
        self.assertTrue(self.store.exists("nasdaq/candles/AAPL/1m.json"))
        self.assertEqual(self.store.read_json("nasdaq/candles/AAPL/1m.json"),
                         {"a": 1})
        self.assertEqual(self.store.list_keys("nasdaq/candles/AAPL"),
                         ["nasdaq/candles/AAPL/1m.json"])
        with self.assertRaises(KeyError):
            self.store.read_json("nasdaq/candles/MSFT/1m.json")
        with self.assertRaises(ValueError):
            self.store.write_json("../escape.json", {})

    def test_write_and_read_symbol_keeps_pr393_shape(self):
        provenance = self._write_aapl()
        content = cache_mod.read_interval(self.store, "AAPL", "1m")
        self.assertEqual(content["schema_version"], 1)
        self.assertEqual(content["dataset"], "single_stock_candles")
        self.assertTrue(content["base"])
        self.assertIsNone(content["derived_from"])
        self.assertEqual(content["bars_count"], 30)
        derived = cache_mod.read_interval(self.store, "AAPL", "5m")
        self.assertFalse(derived["base"])
        self.assertEqual(derived["derived_from"], "1m")
        self.assertEqual(provenance["schema"], "snapshot_provenance.v1")
        self.assertEqual(provenance["envelope"]["files"]["1m"]["origin"],
                         "recorded")
        self.assertEqual(provenance["envelope"]["files"]["1m"]["revision"], 1)

    def test_read_bases_rebuilds_derived(self):
        self._write_aapl()
        bases = cache_mod.read_bases(self.store, "AAPL")
        self.assertEqual(set(bases), {"1m", "1h", "1d"})
        derived = resample_mod.derive_all(bases)
        stored = cache_mod.read_interval(self.store, "AAPL", "5m")
        self.assertEqual(derived["5m"], stored["bars"])

    def test_stamped_bars_carry_envelope_without_mutating_file(self):
        self._write_aapl()
        rows = cache_mod.stamped_bars(self.store, "AAPL", "1m")
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]["origin"], "recorded")
        self.assertEqual(rows[0]["availableAt"], 1795479152000)
        content = cache_mod.read_interval(self.store, "AAPL", "1m")
        self.assertNotIn("eventAt", content["bars"][0])

    def test_read_as_of_stale_is_reported_not_refreshed(self):
        self._write_aapl()
        result = cache_mod.read_as_of(
            self.store, "AAPL", "1m", cutoff="2026-11-24T00:12:32Z",
            session="2026-11-23", max_age_ms=60_000)
        self.assertEqual(result["reading"]["state"], "stale")
        self.assertEqual(len(result["bars"]), 30)  # bars still shown, labelled

    def test_read_as_of_missing_file_is_unavailable(self):
        result = cache_mod.read_as_of(
            self.store, "MSFT", "1m", cutoff="2026-11-24T00:12:32Z",
            max_age_ms=60_000)
        self.assertEqual(result["reading"]["state"], "unavailable")
        self.assertEqual(result["bars"], [])
        self.assertIn("unavailable", result["labels"])

    def test_read_as_of_before_first_bar_is_unavailable(self):
        self._write_aapl()
        result = cache_mod.read_as_of(
            self.store, "AAPL", "1m", cutoff="2026-11-23T14:00:00Z",
            session="2026-11-23", max_age_ms=10 ** 12)
        self.assertEqual(result["reading"]["state"], "unavailable")
        self.assertEqual(result["bars"], [])

    def test_verify_tree_ok_and_catches_tamper(self):
        self._write_aapl()
        result = cache_mod.verify_tree(self.store, "AAPL")
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(len(result["checked"]), 11)
        tampered = cache_mod.read_interval(self.store, "AAPL", "1m")
        tampered["bars"] = tampered["bars"][:-1]
        self.store.write_json("nasdaq/candles/AAPL/1m.json", tampered)
        result = cache_mod.verify_tree(self.store, "AAPL")
        self.assertFalse(result["ok"])
        self.assertTrue(any("1m" in p for p in result["problems"]))

    def test_verify_tree_missing_sidecar(self):
        result = cache_mod.verify_tree(self.store, "AAPL")
        self.assertFalse(result["ok"])
        self.assertIn("missing provenance sidecar", result["problems"])

    def test_cache_age(self):
        self._write_aapl()
        age = cache_mod.cache_age_ms(self.store, "AAPL", "1m",
                                     now="2026-11-24T00:12:32Z")
        self.assertGreaterEqual(age, 0)
        self.assertIsNone(cache_mod.cache_age_ms(self.store, "MSFT", "1m"))


class PublishTest(unittest.TestCase):
    def test_publish_uses_fake_client_and_sidecar_last(self):
        calls = []

        class FakeClient:
            def put_object(self, Bucket, Key, Body, ContentType):
                calls.append(Key)

        files = {"1m": {"bars": []}, "1d": {"bars": []}}
        provenance = {"schema": "snapshot_provenance.v1"}
        result = store_mod.publish_symbol(
            "AAPL", files, provenance,
            config={"R2_ENDPOINT_URL": "https://example.invalid",
                    "R2_ACCESS_KEY_ID": "x", "R2_SECRET_ACCESS_KEY": "y",
                    "TIINGO_R2_BUCKET": "qqq-options-chain-data"},
            client=FakeClient())
        self.assertEqual(result["bucket"], "qqq-options-chain-data")
        self.assertEqual(calls[-1], "nasdaq/candles/AAPL/provenance.json")

    def test_missing_r2_config_fails_closed(self):
        with self.assertRaises(RuntimeError):
            store_mod.read_r2_config(env={})


class SeedTest(unittest.TestCase):
    def test_seed_file_shape_and_counts(self):
        with open(SEED_PATH, encoding="utf-8") as handle:
            seed = json.load(handle)
        self.assertEqual(seed["seed"]["symbol"], "AAPL")
        self.assertEqual(seed["seed"]["origin"], "recorded")
        self.assertIn("NOT synthetic", seed["seed"]["note"])
        self.assertEqual(len(seed["intervals"]["1m"]["bars"]), 390)
        self.assertEqual(len(seed["intervals"]["1h"]["bars"]), 300)
        self.assertEqual(len(seed["intervals"]["1d"]["bars"]), 300)
        self.assertEqual(seed["intervals"]["1m"]["bars"][0]["t"],
                         "2026-11-23T14:30:00Z")
        self.assertEqual(seed["intervals"]["1m"]["bars"][-1]["t"],
                         "2026-11-23T20:59:00Z")
        self.assertEqual(seed["intervals"]["1d"]["bars"][-1]["t"],
                         "2026-11-23T00:00:00Z")
        # Daily bars carry volume; intraday bars do not.
        self.assertIn("volume", seed["intervals"]["1d"]["bars"][-1])
        self.assertNotIn("volume", seed["intervals"]["1m"]["bars"][0])

    def test_seed_command_writes_verifiable_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Args:
                out = tmp
            self.assertEqual(cmd_seed(Args()), 0)
            store = store_mod.LocalDirectoryStore(tmp)
            result = cache_mod.verify_tree(store, "AAPL")
            self.assertTrue(result["ok"], result["problems"])
            rows = cache_mod.stamped_bars(store, "AAPL", "1d")
            self.assertEqual(len(rows), 300)
            self.assertEqual(rows[-1]["value"]["close"], 349.08)


if __name__ == "__main__":
    unittest.main()
