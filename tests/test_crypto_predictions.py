"""Offline tests for hourly crypto predictions.

No provider calls, no R2, no clock dependence beyond fixed fixtures::

    python -m pytest tests/test_crypto_predictions.py -q
    python -m unittest tests.test_crypto_predictions -v

Covers the estimator (pure function: range contains the estimate,
direction consistent with momentum sign, confidence bounds, too
few bars, BTC correlation), the One Clock envelope (origin
synthetic, retention after the target hour — a passed prediction
stays readable with its value intact, as-of selection, live vs
estimate cohorts never mixing), the prediction file round-trip,
the producer (from cached 1h bars, CoinGecko-style absence,
history carry-forward), the provenance sidecar and publish keys,
the CLI seed command (byte-identical to the checked-in seed) —
plus the checked-in BTC seed (real Coinbase bars, real computed
estimates).
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_data.crypto_candle_cache import one_clock as oc  # noqa: E402
from market_data.crypto_predictions import estimator as est_mod  # noqa: E402
from market_data.crypto_predictions import predictions as pred_mod  # noqa: E402
from market_data.crypto_predictions import producer as producer_mod  # noqa: E402
from market_data.crypto_predictions import provenance as prov_mod  # noqa: E402
from market_data.crypto_predictions import publish as publish_mod  # noqa: E402
from market_data.crypto_predictions import cli as cli_mod  # noqa: E402

UTC = timezone.utc


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _bars(start, closes, symbol="BTC"):
    """Fixture 1h bars from a list of closes (oldest first).

    Each bar's open is the previous close, high/low straddle the
    open/close by a fixed 0.1% — deterministic fixtures, not real
    prices.
    """
    bars = []
    previous = closes[0]
    # Observed just after the last bar's close: every fixture bar is
    # completed at any now at or after that instant.
    observed = _iso(start + timedelta(hours=len(closes), minutes=5))
    for i, close in enumerate(closes):
        moment = start + timedelta(hours=i)
        high = max(previous, close) * 1.001
        low = min(previous, close) * 0.999
        bars.append({"t": _iso(moment), "open": previous, "high": high,
                     "low": low, "close": close, "volume": 10.0,
                     "observed_at": observed,
                     "origin": "recorded"})
        previous = close
    return bars


def _up_bars(n=30, start=None):
    # Steady +0.4%/hour climb.
    start = start or datetime(2026, 11, 23, 0, 0, tzinfo=UTC)
    closes = [100.0 * (1.004 ** i) for i in range(n)]
    return _bars(start, closes)


def _down_bars(n=30, start=None):
    start = start or datetime(2026, 11, 23, 0, 0, tzinfo=UTC)
    closes = [100.0 * (0.996 ** i) for i in range(n)]
    return _bars(start, closes)


def _flat_bars(n=30, start=None):
    start = start or datetime(2026, 11, 23, 0, 0, tzinfo=UTC)
    return _bars(start, [100.0] * n)


def _ms(iso):
    return oc.instant_ms(iso, "t")


class EstimatorTests(unittest.TestCase):
    def test_range_contains_estimate_and_direction_follows_momentum(self):
        for bars, direction in ((_up_bars(), "up"), (_down_bars(), "down")):
            est = est_mod.estimate(bars, symbol="BTC")
            self.assertIsNotNone(est)
            self.assertLess(est["range_low"], est["estimated_price"])
            self.assertLess(est["estimated_price"], est["range_high"])
            self.assertEqual(est["direction"], direction)
            self.assertEqual(est["basis"][0], "last-hour momentum")
            self.assertIn("hourly volatility", est["basis"])
            self.assertIn("last-hour range", est["basis"])

    def test_flat_bars_estimate_flat(self):
        est = est_mod.estimate(_flat_bars(), symbol="BTC")
        self.assertEqual(est["direction"], "flat")
        self.assertAlmostEqual(est["pct_move"], 0.0)
        self.assertAlmostEqual(est["estimated_price"], 100.0)
        self.assertGreaterEqual(est["confidence"], 0.05)
        self.assertLessEqual(est["confidence"], 0.95)

    def test_confidence_bounds_and_completeness(self):
        few = est_mod.estimate(_up_bars(n=5), symbol="BTC")
        many = est_mod.estimate(_up_bars(n=30), symbol="BTC")
        self.assertGreaterEqual(few["confidence"], 0.05)
        self.assertLessEqual(few["confidence"], 0.95)
        # Fewer bars in the window means lower completeness, hence
        # no higher confidence than the full-window estimate.
        self.assertLessEqual(few["confidence"], many["confidence"])
        self.assertEqual(few["data_window"]["returns"], 4)
        self.assertEqual(many["data_window"]["returns"], 24)

    def test_too_few_bars_returns_none(self):
        self.assertIsNone(est_mod.estimate([], symbol="BTC"))
        self.assertIsNone(est_mod.estimate(
            _flat_bars(n=1), symbol="BTC"))

    def test_btc_correlation_present_for_other_coins(self):
        btc = _up_bars()
        eth = _up_bars()
        est = est_mod.estimate(eth, btc_bars=btc, symbol="ETH")
        self.assertIsNotNone(est["signals"]["btc_correlation"])
        self.assertAlmostEqual(est["signals"]["btc_correlation"], 1.0, places=6)
        self.assertIn("BTC correlation", est["basis"])

    def test_btc_correlation_absent_for_btc_itself(self):
        est = est_mod.estimate(_up_bars(), symbol="BTC")
        self.assertIsNone(est["signals"]["btc_correlation"])
        self.assertNotIn("BTC correlation", est["basis"])

    def test_estimate_uses_only_completed_bars(self):
        # The caller passes completed bars; a bar appended after
        # the last close changes the estimate, so the window is
        # exactly the bars handed in.
        short = _up_bars(n=25)
        longer = _up_bars(n=26)
        self.assertNotEqual(est_mod.estimate(short, symbol="BTC")["estimated_price"],
                            est_mod.estimate(longer, symbol="BTC")["estimated_price"])


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.est = est_mod.estimate(_up_bars(), symbol="BTC")
        self.target_ms = _ms("2026-11-24T06:00:00Z")
        self.generated_at = "2026-11-24T06:05:00Z"

    def test_origin_is_synthetic_and_envelope_is_real_one_clock(self):
        row = pred_mod.prediction_to_observation(
            "BTC", self.est, self.target_ms, self.generated_at)
        self.assertEqual(row["source"], "crypto_predictions")
        self.assertEqual(row["session"], "continuous")
        self.assertEqual(row["instrument"], "BTC")
        self.assertEqual(row["origin"], "synthetic")
        self.assertEqual(row["revision"], 1)
        self.assertEqual(row["id"], "BTC|1h|2026-11-24T06:00:00Z")
        self.assertEqual(row["eventAt"], self.target_ms)
        self.assertEqual(row["valueAt"], self.target_ms)
        self.assertEqual(row["availableAt"], _ms(self.generated_at))
        self.assertEqual(row["value"]["direction"], "up")
        self.assertEqual(row["evidence"], "crypto/predictions/BTC.json")

    def test_availability_before_event_is_rejected(self):
        with self.assertRaises(TypeError):
            pred_mod.prediction_to_observation(
                "BTC", self.est, self.target_ms, "2026-11-24T05:59:00Z")

    def test_no_retraction_helper_exists_and_null_is_rejected(self):
        # Retention, not retraction: there is no revision-2 null
        # row to publish, and a null estimate is refused outright.
        self.assertFalse(hasattr(pred_mod, "retraction_observation"))
        with self.assertRaises(ValueError):
            pred_mod.prediction_to_observation(
                "BTC", None, self.target_ms, self.generated_at)

    def test_passed_prediction_is_retained_with_value_intact(self):
        # The target hour (06:00–07:00) has passed at the 07:30
        # cutoff, but the prediction stays readable with its value
        # intact: readingAsOf reports it fresh (age 90 minutes,
        # inside the 2h bound), and a reader judges it by its
        # eventAt / target window, which mark it as past.
        value_row = pred_mod.prediction_to_observation(
            "BTC", self.est, self.target_ms, self.generated_at)
        rows = [value_row]
        selected = oc.select_as_of(rows, {
            "session": "continuous", "instrument": "BTC",
            "cutoff": _ms("2026-11-24T07:30:00Z"), "origin": "synthetic"})
        self.assertEqual(len(selected), 1)
        self.assertIsNotNone(selected[0]["value"])
        self.assertEqual(selected[0]["value"]["direction"], "up")
        reading = pred_mod.current_prediction(
            rows, "BTC", _ms("2026-11-24T07:30:00Z"), 2 * 60 * 60 * 1000)
        self.assertEqual(reading["state"], "fresh")
        self.assertEqual(reading["row"]["id"], "BTC|1h|2026-11-24T06:00:00Z")
        self.assertIsNotNone(reading["row"]["value"])
        # Past the staleness bound the same row is stale, still
        # with its value intact — never unavailable merely
        # because the hour passed.
        stale = pred_mod.current_prediction(
            rows, "BTC", _ms("2026-11-24T08:30:00Z"), 2 * 60 * 60 * 1000)
        self.assertEqual(stale["state"], "stale")
        self.assertIsNotNone(stale["row"]["value"])
        self.assertEqual(stale["row"]["value"]["direction"], "up")

    def test_select_as_of_returns_prediction_available_at_cutoff_only(self):
        first = pred_mod.prediction_to_observation(
            "BTC", self.est, self.target_ms, self.generated_at)
        later_est = est_mod.estimate(_up_bars(), symbol="BTC")
        second = pred_mod.prediction_to_observation(
            "BTC", later_est, self.target_ms + 3600_000, "2026-11-24T07:05:00Z")
        rows = [first, second]
        early = oc.select_as_of(rows, {
            "session": "continuous", "instrument": "BTC",
            "cutoff": _ms("2026-11-24T06:30:00Z"), "origin": "synthetic"})
        self.assertEqual([r["id"] for r in early], [first["id"]])
        late = oc.select_as_of(rows, {
            "session": "continuous", "instrument": "BTC",
            "cutoff": _ms("2026-11-24T07:30:00Z"), "origin": "synthetic"})
        self.assertEqual(len(late), 2)

    def test_live_and_estimate_cohorts_never_mix(self):
        pred_row = pred_mod.prediction_to_observation(
            "BTC", self.est, self.target_ms, self.generated_at)
        live_bar = {"t": "2026-11-24T05:00:00Z", "open": 100, "high": 101,
                    "low": 99, "close": 100.5, "volume": 5,
                    "observed_at": "2026-11-24T06:05:00Z", "origin": "recorded"}
        live_row = oc.bar_to_observation(
            live_bar, "BTC", "1h", "crypto/candles/BTC/1h.json")
        rows = [pred_row, live_row]
        recorded = oc.select_as_of(rows, {
            "session": "continuous", "instrument": "BTC",
            "cutoff": _ms("2026-11-24T06:30:00Z"), "origin": "recorded"})
        self.assertEqual([r["origin"] for r in recorded], ["recorded"])
        synthetic = oc.select_as_of(rows, {
            "session": "continuous", "instrument": "BTC",
            "cutoff": _ms("2026-11-24T06:30:00Z"), "origin": "synthetic"})
        self.assertEqual([r["origin"] for r in synthetic], ["synthetic"])
        self.assertNotEqual(recorded[0]["id"], synthetic[0]["id"])

    def test_expired_target_hour_detection(self):
        self.assertFalse(pred_mod.is_expired(
            self.target_ms, self.target_ms + 3599_999))
        self.assertTrue(pred_mod.is_expired(
            self.target_ms, self.target_ms + 3600_000))


class FileTests(unittest.TestCase):
    def test_build_and_read_round_trip(self):
        import tempfile
        est = est_mod.estimate(_up_bars(), symbol="BTC")
        target_ms = _ms("2026-11-24T06:00:00Z")
        rows = [pred_mod.prediction_to_observation(
            "BTC", est, target_ms, "2026-11-24T06:05:00Z")]
        content = pred_mod.build_prediction_file(
            "BTC", rows, "2026-11-24T06:05:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            path = pred_mod.write_prediction_file(tmp, "BTC", content)
            read = pred_mod.read_prediction_file(path)
        self.assertEqual(read["dataset"], "crypto_predictions")
        self.assertEqual(read["observations_count"], 1)
        self.assertEqual(read["observations"][0]["id"], rows[0]["id"])

    def test_non_synthetic_row_rejected_on_read(self):
        import tempfile
        est = est_mod.estimate(_up_bars(), symbol="BTC")
        target_ms = _ms("2026-11-24T06:00:00Z")
        row = pred_mod.prediction_to_observation(
            "BTC", est, target_ms, "2026-11-24T06:05:00Z")
        row = dict(row, origin="recorded")
        content = {"schema_version": 1, "dataset": "crypto_predictions",
                   "symbol": "BTC", "horizon": "1h",
                   "generated_at": "2026-11-24T06:05:00Z",
                   "coverage": "current", "reason": None,
                   "observations_count": 1, "observations": [row]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTC.json"
            path.write_text(json.dumps(content), encoding="utf-8")
            with self.assertRaises(ValueError):
                pred_mod.read_prediction_file(path)

    def test_seed_file_loads_and_live_prediction_is_the_latest(self):
        seed = ROOT / "market_data" / "crypto_predictions" / "seed" / "BTC" / "predictions.json"
        content = pred_mod.read_prediction_file(seed)
        self.assertEqual(content["symbol"], "BTC")
        self.assertEqual(content["coverage"], "current")
        # Seven value rows (one per target hour), no retractions:
        # the six passed hours are retained as historical evidence.
        self.assertEqual(content["observations_count"], 7)
        live = [r for r in content["observations"] if r["value"] is not None]
        self.assertEqual(len(live), 7)
        self.assertTrue(all(r["revision"] == 1
                            for r in content["observations"]))
        latest = live[-1]
        self.assertEqual(latest["value"]["target_start"], "2026-11-24T00:00:00Z")
        # Every passed target hour is retained, and the latest
        # row is the one the selector returns for it.
        reading = pred_mod.current_prediction(
            content["observations"], "BTC",
            _ms("2026-11-24T00:45:00Z"), 2 * 60 * 60 * 1000)
        self.assertEqual(reading["state"], "fresh")
        self.assertEqual(reading["row"]["id"], latest["id"])
        self.assertAlmostEqual(reading["row"]["value"]["estimated_price"],
                               61192.0, delta=1.0)
        self.assertEqual(reading["row"]["value"]["last_close"], 61189.97)
        # At 02:30 the 00:00 hour has passed; the reading is stale
        # by age, with its value intact — the seed is a snapshot,
        # and the producer would carry the row forward as history
        # on its next run.
        stale = pred_mod.current_prediction(
            content["observations"], "BTC",
            _ms("2026-11-24T02:30:00Z"), 2 * 60 * 60 * 1000)
        self.assertEqual(stale["state"], "stale")
        self.assertIsNotNone(stale["row"]["value"])
        self.assertAlmostEqual(stale["row"]["value"]["estimated_price"],
                               61192.0, delta=1.0)


class ProducerTests(unittest.TestCase):
    def _cache_dir(self, tmp, bars_by_symbol):
        for symbol, bars in bars_by_symbol.items():
            path = Path(tmp) / "crypto" / "candles" / symbol
            path.mkdir(parents=True, exist_ok=True)
            content = {"schema_version": 1, "dataset": "crypto_candles",
                       "symbol": symbol, "interval": "1h", "base": True,
                       "derived_from": None, "source": "coinbase",
                       "source_label": "test", "bars_count": len(bars),
                       "first_bar": bars[0]["t"], "last_bar": bars[-1]["t"],
                       "generated_at": "2026-11-24T06:00:00Z", "bars": bars}
            (path / "1h.json").write_text(json.dumps(content), encoding="utf-8")
        return tmp

    def test_build_from_cached_1h_bars(self):
        import tempfile
        bars = _up_bars()
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = self._cache_dir(tmp, {"BTC": bars})
            now_ms = _ms("2026-11-24T06:05:00Z")
            content, status = producer_mod.build_symbol(
                "BTC", str(cache_dir), "2026-11-24T06:05:00Z", now_ms)
        self.assertEqual(status, "current")
        self.assertEqual(len(content["observations"]), 1)
        row = content["observations"][0]
        # Last fixture bar starts 2026-11-24T05:00, closes 06:00.
        self.assertEqual(row["id"], "BTC|1h|2026-11-24T06:00:00Z")
        self.assertEqual(row["value"]["direction"], "up")

    def test_forming_bar_never_enters_the_window(self):
        import tempfile
        bars = _up_bars()
        forming = dict(bars[-1])
        forming["t"] = "2026-11-24T06:00:00Z"
        forming["observed_at"] = "2026-11-24T06:05:00Z"
        bars = bars + [forming]
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = self._cache_dir(tmp, {"BTC": bars})
            now_ms = _ms("2026-11-24T06:05:00Z")
            content, status = producer_mod.build_symbol(
                "BTC", str(cache_dir), "2026-11-24T06:05:00Z", now_ms)
        # The forming 06:00 bar closes at 07:00, after now: the
        # estimate still targets 06:00 from the completed bars.
        self.assertEqual(content["observations"][0]["id"],
                         "BTC|1h|2026-11-24T06:00:00Z")

    def test_coingecko_style_absence_no_1h_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = self._cache_dir(tmp, {"BTC": _up_bars()})
            now_ms = _ms("2026-11-24T06:05:00Z")
            content, status = producer_mod.build_symbol(
                "USDT", str(cache_dir), "2026-11-24T06:05:00Z", now_ms)
        self.assertEqual(status, "absent")
        self.assertEqual(content["coverage"], "absent")
        self.assertEqual(content["observations"], [])
        self.assertIn("no completed hourly", content["reason"])

    def test_history_carried_forward_from_previous_file(self):
        import tempfile
        bars = _up_bars()
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = self._cache_dir(tmp, {"BTC": bars})
            now_ms = _ms("2026-11-24T06:05:00Z")
            first, _ = producer_mod.build_symbol(
                "BTC", str(cache_dir), "2026-11-24T06:05:00Z", now_ms)
            # One hour later the cache has one more bar; the old
            # target hour has passed and is retained as history.
            more = _up_bars(n=31)
            cache_dir = self._cache_dir(tmp, {"BTC": more})
            later_ms = _ms("2026-11-24T07:05:00Z")
            second, status = producer_mod.build_symbol(
                "BTC", str(cache_dir), "2026-11-24T07:05:00Z", later_ms,
                previous=first)
        self.assertEqual(status, "current")
        ids = [(r["id"], r["revision"], r["value"] is None)
               for r in second["observations"]]
        self.assertIn(("BTC|1h|2026-11-24T06:00:00Z", 1, False), ids)
        self.assertIn(("BTC|1h|2026-11-24T07:00:00Z", 1, False), ids)
        self.assertNotIn(("BTC|1h|2026-11-24T06:00:00Z", 2, True), ids)
        self.assertTrue(all(r["value"] is not None
                            for r in second["observations"]))
        # The passed 06:00 prediction is still readable as of a
        # cutoff inside its hour — and still readable afterwards
        # as history: select_as_of at 07:05 returns both rows,
        # the 06:00 value intact.
        past = oc.select_as_of(second["observations"], {
            "session": "continuous", "instrument": "BTC",
            "cutoff": later_ms, "origin": "synthetic"})
        self.assertEqual([r["id"] for r in past],
                         ["BTC|1h|2026-11-24T06:00:00Z",
                          "BTC|1h|2026-11-24T07:00:00Z"])
        self.assertTrue(all(r["value"] is not None for r in past))
        reading = pred_mod.current_prediction(
            second["observations"], "BTC", later_ms, 2 * 60 * 60 * 1000)
        self.assertEqual(reading["row"]["id"], "BTC|1h|2026-11-24T07:00:00Z")

    def test_late_run_publishes_historical_coverage(self):
        # A producer run landing after its target hour has already
        # passed publishes the estimate as historical evidence:
        # coverage "historical", value intact, no retraction.
        import tempfile
        bars = _up_bars()
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = self._cache_dir(tmp, {"BTC": bars})
            late_ms = _ms("2026-11-24T07:05:00Z")
            content, status = producer_mod.build_symbol(
                "BTC", str(cache_dir), "2026-11-24T07:05:00Z", late_ms)
        self.assertEqual(status, "historical")
        self.assertEqual(content["coverage"], "historical")
        self.assertEqual(len(content["observations"]), 1)
        self.assertIsNotNone(content["observations"][0]["value"])
        self.assertEqual(content["observations"][0]["id"],
                         "BTC|1h|2026-11-24T06:00:00Z")

    def test_cli_preserves_first_issue_across_retries_new_hours_and_input_outages(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = self._cache_dir(tmp, {"BTC": _up_bars()})
            out = Path(tmp) / "output"
            path = pred_mod.prediction_path(out, "BTC")
            def run(at):
                cli_mod.main(["predict", "--cache-dir", str(cache_dir), "--out", str(out),
                              "--symbols", "BTC", "--generated-at", at])
                return pred_mod.read_prediction_file(path)

            first, = run("2026-11-24T06:05:00Z")["observations"]
            # Even revised inputs cannot rewrite a prediction that was already issued.
            self._cache_dir(tmp, {"BTC": _down_bars()})
            self.assertEqual(run("2026-11-24T06:30:00Z")["observations"], [first])
            self._cache_dir(tmp, {"BTC": _up_bars(31)})
            issued = run("2026-11-24T07:05:00Z")["observations"]
            self.assertEqual(issued[0], first)
            self.assertEqual(len(issued), 2)
            (Path(tmp) / "crypto/candles/BTC/1h.json").unlink()
            gap = run("2026-11-24T08:00:00Z")
            self.assertEqual(gap["coverage"], "historical")
            self.assertEqual(gap["observations"], issued)
            expired = run("2026-12-02T08:00:00Z")
            self.assertEqual(expired["coverage"], "absent")
            self.assertEqual(expired["observations"], [])

    def test_archive_bars_must_have_arrived_and_final_pair_must_be_consecutive(self):
        import tempfile
        bars = _up_bars()
        with tempfile.TemporaryDirectory() as tmp:
            self._cache_dir(tmp, {"BTC": bars})
            early = "2026-11-24T06:04:59Z"
            _, state = producer_mod.build_symbol("BTC", tmp, early, _ms(early))
            self.assertEqual(state, "absent")
            bars.pop(-2)
            self._cache_dir(tmp, {"BTC": bars})
            received = "2026-11-24T06:05:00Z"
            _, state = producer_mod.build_symbol("BTC", tmp, received, _ms(received))
            self.assertEqual(state, "absent")

    def test_archive_cli_requires_received_closed_candles_and_a_live_target(self):
        from market_data.crypto_candle_cache.archive import install
        import tempfile
        import zipfile

        now = datetime(2026, 11, 24, 7, 5, tzinfo=UTC)
        hours = [now.replace(hour=h, minute=0) for h in (4, 5, 6, 7)]
        coverage = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fixture.zip"
            with zipfile.ZipFile(source, "w") as z:
                for symbol, provider in (("BTC", "coinbase"), ("TRX", "coingecko")):
                    if provider == "coinbase":
                        raw = [[int(t.timestamp()), 100, 102, 100, 101, 5] for t in hours]
                    else:
                        raw = [{"timestamp_ms": int(t.timestamp()*1000), "date": _iso(t),
                                "price": 15} for t in hours]
                    coverage[symbol] = {"1hour": dict(source=provider, bars=len(raw),
                                                       first=_iso(hours[0]), last=_iso(hours[-1]),
                                                       updated=_iso(now), done=True)}
                    z.writestr(f"data/{provider}/{symbol}_1hour.json", json.dumps(raw))
                # A simultaneous daily price sample from another provider cannot
                # stand in for an hourly Coinbase candle in the estimator.
                day = now.replace(hour=0, minute=0)
                sample = [{"timestamp_ms": int(day.timestamp()*1000),
                           "date": _iso(day), "price": 999}]
                coverage["BTC"]["daily"] = dict(source="coingecko", bars=1, first=_iso(day),
                                                last=_iso(day), updated=_iso(now), done=True)
                z.writestr("data/coingecko/BTC_daily.json", json.dumps(sample))
                z.writestr("manifest.json", json.dumps(dict(coins=list(coverage),
                                                            coverage=coverage, started=_iso(hours[-1]), updated=_iso(now))))
            with mock.patch("market_data.crypto_candle_cache.archive.datetime", wraps=datetime) as clock:
                clock.now.return_value = now
                db = install(source, root / "snapshots")
            def run(at, subdir):
                output = root / subdir
                cli_mod.main(["predict", "--archive", str(db), "--out", str(output),
                              "--generated-at", _iso(at)])
                return pred_mod.prediction_path(output, "BTC")
            self.assertFalse(run(now-timedelta(seconds=1), "early").exists())
            first, = pred_mod.read_prediction_file(run(now, "issued"))["observations"]
            self.assertEqual(first["eventAt"], int(hours[-1].timestamp()*1000))
            self.assertEqual(first["availableAt"], int(now.timestamp()*1000))
            self.assertEqual(first["input_archive_sha256"], db.stem)
            self.assertEqual(first["value"]["data_window"]["bars_1h"], 3)
            self.assertEqual(first["value"]["last_close"], 101)
            self.assertFalse(pred_mod.prediction_path(root/"issued", "TRX").exists())
            self.assertFalse(run(now + timedelta(hours=1), "late").exists())
            retained = pred_mod.read_prediction_file(run(now + timedelta(hours=1), "issued"))
            self.assertEqual(retained["coverage"], "historical")
            self.assertEqual(retained["observations"], [first])

    def test_unknown_symbol_rejected(self):
        with self.assertRaises(ValueError):
            producer_mod.build_all("/nonexistent", "2026-11-24T06:05:00Z",
                                   _ms("2026-11-24T06:05:00Z"),
                                   symbols=["NOPE"])


class ProvenanceAndPublishTests(unittest.TestCase):
    def test_provenance_sidecar(self):
        est = est_mod.estimate(_up_bars(), symbol="BTC")
        row = pred_mod.prediction_to_observation(
            "BTC", est, _ms("2026-11-24T06:00:00Z"), "2026-11-24T06:05:00Z")
        content = pred_mod.build_prediction_file(
            "BTC", [row], "2026-11-24T06:05:00Z")
        sidecar = prov_mod.build_provenance(
            "BTC", content, generated_at="2026-11-24T06:05:00Z",
            publication_status="not_published",
            publication_reason="local run: --publish not given")
        self.assertEqual(sidecar["schema"], "snapshot_provenance.v1")
        self.assertEqual(sidecar["dataset"], "crypto_predictions")
        self.assertEqual(sidecar["coverage"]["live_predictions"], 1)
        self.assertEqual(sidecar["coverage"]["historical"], 0)
        self.assertEqual(sidecar["files"]["predictions"]["historical"], 0)
        self.assertEqual(sidecar["publication"]["status"], "not_published")
        self.assertEqual(prov_mod.sidecar_key("BTC"),
                         "crypto/predictions/BTC/provenance.json")

    def test_publish_keys_and_config(self):
        class FakeClient:
            def __init__(self):
                self.puts = []

            def put_object(self, Bucket, Key, Body, ContentType):
                self.puts.append((Bucket, Key))

        config = {"R2_ENDPOINT_URL": "https://example.invalid",
                  "R2_ACCESS_KEY_ID": "id", "R2_SECRET_ACCESS_KEY": "secret",
                  "CRYPTO_R2_BUCKET": "bucket"}
        client = FakeClient()
        est = est_mod.estimate(_up_bars(), symbol="BTC")
        row = pred_mod.prediction_to_observation(
            "BTC", est, _ms("2026-11-24T06:00:00Z"), "2026-11-24T06:05:00Z")
        content = pred_mod.build_prediction_file(
            "BTC", [row], "2026-11-24T06:05:00Z")
        sidecar = prov_mod.build_provenance("BTC", content)
        result = publish_mod.publish_symbol(
            "BTC", content, sidecar, config=config, client=client)
        self.assertEqual(result["keys"],
                         ["crypto/predictions/BTC.json",
                          "crypto/predictions/BTC/provenance.json"])
        with self.assertRaises(RuntimeError):
            publish_mod.read_config(env={})


class CliTests(unittest.TestCase):
    def test_seed_command_reproduces_checked_in_seed(self):
        import tempfile
        seed = ROOT / "market_data" / "crypto_predictions" / "seed" / "BTC" / "predictions.json"
        expected = seed.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli_mod.main(["seed", "--out", tmp]), 0)
            produced = (Path(tmp) / "crypto" / "predictions" / "BTC.json"
                        ).read_text(encoding="utf-8")
        self.assertEqual(produced, expected)

    def test_universe_lists_fifty(self):
        import io
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli_mod.main(["universe"])
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 51)
        self.assertTrue(lines[0].startswith("  1 BTC"))


if __name__ == "__main__":
    unittest.main()
