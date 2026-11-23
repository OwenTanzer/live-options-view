"""Reference market readings: parity, coverage, join and publication (#107).

No bot, account or network is involved in the reading itself. The tests check
the seven G4 reading families against the merged #110 ownership decisions:
VWAP, RVOL and reference momentum are published from the same
`underlying_market` values; PCR, max pain and the 2% reference OI skew are
computed from the board rows with strategy parity; trading momentum stays
per-account and the snapshot-IV/Black-Scholes comparison stays buy-only, but
every decision record joins the exact snapshot/readings digest instead of a
mutable latest object.

    python -m unittest discover -s tests -p test_market_readings.py -v
"""

import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "crassus"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import market_data.live_market as collector  # noqa: E402
from market_data.overview import readings as readings_module  # noqa: E402
from market_data.overview.readings import (  # noqa: E402
    build_snapshot_readings, compute_reference_max_pain, compute_reference_oi_skew,
    compute_reference_pcr, readings_digest,
)
from crassus.client import Book  # noqa: E402
from crassus.market import MarketSnapshot  # noqa: E402
from crassus.runner import decision_base  # noqa: E402
from crassus.strategies import oi_skew  # noqa: E402
from crassus.strategies.max_pain import _compute_max_pain  # noqa: E402
from crassus.strategies.put_call_ratio import compute_pcr  # noqa: E402
from crassus.strategy import StrategyContext  # noqa: E402

UTC = timezone.utc
NOW = datetime.now(UTC)
TODAY = NOW.date()
EXP = TODAY.isoformat()
KEY = "intraday/20261109/snapshot_120000000000.csv"


def row(strike, kind, oi, bid=1.0, ask=1.2):
    letter = "C" if kind == "call" else "P"
    return {
        "OptionSymbol": f"QQQ261109{letter}{int(strike * 1000):08d}",
        "Strike": float(strike), "Type": kind, "Bid": bid, "Ask": ask,
        "OpenInterest": oi,
    }


def chain(underlying=600.0):
    rows = []
    for strike in (590, 595, 600, 605, 610):
        rows.append(row(strike, "call", 100 + (strike - 590) * 10))
        rows.append(row(strike, "put", 300 - (strike - 590) * 10))
    return rows


def market_block():
    ts = NOW.isoformat()
    return {
        "spot": 600.1, "spot_ts": ts, "vwap": 599.9, "vwap_ts": ts,
        "vwap_partial_session": False, "vwap_session_started_at": ts,
        "price_vs_vwap_abs": 0.2, "price_vs_vwap_pct": 0.0333,
        "session_volume": 1_000_000, "session_volume_ts": ts,
        "rvol": {"status": "ok", "multiple": 1.25, "baseline_volume": 800_000,
                 "baseline_days_used": 12, "baseline_lookback_days": 20,
                 "baseline_updated_through": "2026-11-06", "bucket_label": "10:00"},
        "momentum": {"status": "ok", "return_pct": 0.12, "anchor_age_minutes": 58.0,
                     "sample_count": 9, "lookback_minutes": 60,
                     "direction": "up"},
    }


def build(rows=None, market=None, underlying=600.0, parameters=None):
    return build_snapshot_readings(
        rows=rows if rows is not None else chain(), symbol="QQQ", underlying=underlying,
        snapshot_key=KEY, snapshot_timestamp=NOW.isoformat(), calculated_at=NOW,
        freshness="live", observed_at=(NOW - timedelta(seconds=1)).isoformat(),
        market=market if market is not None else market_block(),
        parameters=parameters or {},
    )


class FormulaParity(unittest.TestCase):
    def test_pcr_matches_the_strategy(self):
        rows = chain()
        self.assertEqual(compute_reference_pcr(rows)["values"]["put_call_oi_ratio"],
                         compute_pcr(rows))

    def test_pcr_zero_call_oi_is_undefined_in_both(self):
        rows = [row(600, "put", 10)]
        self.assertIsNone(compute_pcr(rows))
        result = compute_reference_pcr(rows)
        self.assertIsNone(result["values"]["put_call_oi_ratio"])
        self.assertEqual(result["status"], {"code": "undefined", "reason": "zero_call_oi"})

    def test_max_pain_matches_the_strategy_including_ties(self):
        for rows in (chain(), chain(600.0)[::-1]):
            expected_strike, expected_both = _compute_max_pain(rows)
            result = compute_reference_max_pain(rows)
            self.assertEqual(result["values"]["max_pain_strike"], expected_strike)
            self.assertEqual(result["values"]["strikes_with_oi_both_sides"], expected_both)

    def test_max_pain_with_no_open_interest_surface_is_no_data(self):
        # All-zero and all-missing are indistinguishable on this feed; both
        # have no loss surface, so the reading must not publish the lowest
        # candidate strike as a max-pain level.
        zero = [row(600, "call", 0), row(600, "put", 0), row(605, "call", 0)]
        missing = [row(600, "call", None), row(600, "put", None), row(605, "call", None)]
        zero_result = compute_reference_max_pain(zero)
        missing_result = compute_reference_max_pain(missing)
        self.assertEqual(zero_result, missing_result)
        for result in (zero_result, missing_result):
            self.assertEqual(result["status"],
                             {"code": "no_data", "reason": "no_open_interest_surface"})
            self.assertIsNone(result["values"]["max_pain_strike"])
            self.assertEqual(result["values"]["rows_with_open_interest"], 0)
            self.assertEqual(result["values"]["strikes_with_oi_both_sides"], 0)
            self.assertEqual(result["values"]["candidate_strikes"], 2)
        # The strategy's own fallback is unchanged: this is an unsupported
        # reading, not a strategy policy change.
        self.assertEqual(_compute_max_pain(zero)[0], 600)

    def test_max_pain_with_one_sided_open_interest_still_matches(self):
        rows = [row(600, "call", 100), row(605, "call", 50),
                row(600, "put", 0), row(605, "put", 0)]
        expected_strike, expected_both = _compute_max_pain(rows)
        result = compute_reference_max_pain(rows)
        self.assertEqual(result["status"]["code"], "ok")
        self.assertEqual(result["values"]["max_pain_strike"], expected_strike)
        self.assertEqual(result["values"]["strikes_with_oi_both_sides"], expected_both)

    def test_reference_skew_matches_the_strategy_band(self):
        rows = chain()
        call_oi, put_oi, band = oi_skew._near_money_oi(
            type("S", (), {"underlying_price": 600.0, "rows": rows})(), 0.02)
        result = compute_reference_oi_skew(rows, 600.0)
        self.assertEqual(result["values"]["near_money_call_oi"], call_oi)
        self.assertEqual(result["values"]["near_money_put_oi"], put_oi)
        self.assertEqual(result["values"]["band_strikes"], band)
        total = call_oi + put_oi
        self.assertAlmostEqual(result["values"]["imbalance_ratio"], (call_oi - put_oi) / total)


class CoverageAndParameters(unittest.TestCase):
    def test_seven_families_are_covered_with_the_accepted_routes(self):
        built = build()
        self.assertEqual([r["reading_id"] for r in built], [
            "reference_vwap", "reference_rvol", "reference_momentum",
            "reference_pcr", "reference_max_pain", "reference_oi_skew", "qqq_iv_header"])
        by_id = {r["reading_id"]: r for r in built}
        # VWAP/RVOL/reference momentum describe the published underlying_market
        # values; they are not recomputed differently here.
        self.assertEqual(by_id["reference_vwap"]["values"]["vwap"], 599.9)
        self.assertEqual(by_id["reference_rvol"]["values"]["rvol_multiple"], 1.25)
        self.assertEqual(by_id["reference_momentum"]["values"]["return_pct"], 0.12)
        for reading in built:
            self.assertEqual(reading["schema_version"], "market_reading.v1")
            self.assertEqual(reading["interpretation"],
                             {"kind": "reference", "account_alias": None})
            self.assertEqual(reading["input"]["snapshot_key"], KEY)
            self.assertEqual(set(reading["units"]), set(reading["values"]))
            self.assertTrue(reading["calculation"]["version"].endswith(".v1"))

    def test_effective_parameters_are_identified_and_versioned(self):
        built = {r["reading_id"]: r for r in build()}
        rvol = built["reference_rvol"]["calculation"]
        self.assertEqual(rvol["parameters"]["bucket_minutes"], 5)
        self.assertEqual(rvol["parameters"]["lookback_days"], 20)
        self.assertEqual(rvol["parameters"]["min_days_required"], 5)
        self.assertEqual(rvol["parameters"]["effective_days_used"], 12)
        momentum = built["reference_momentum"]["calculation"]["parameters"]
        self.assertEqual(momentum["lookback_minutes"], 60)
        self.assertEqual(momentum["max_anchor_overshoot_minutes"], 10)
        self.assertEqual(momentum["neutral_band_use"], "display_only")
        skew = built["reference_oi_skew"]["calculation"]["parameters"]
        self.assertEqual(skew["near_money_pct"], 0.02)
        self.assertFalse(skew["account_specific"])

    def test_vwap_rvol_momentum_statuses_are_not_flattened(self):
        market = market_block()
        market["vwap"] = None
        market["rvol"] = {"status": "insufficient_history", "multiple": None,
                          "baseline_volume": None, "baseline_days_used": 3}
        market["momentum"] = {"status": "warming_up", "return_pct": None,
                              "anchor_age_minutes": None, "sample_count": 2}
        built = {r["reading_id"]: r for r in build(market=market)}
        self.assertEqual(built["reference_vwap"]["status"]["code"], "no_data")
        self.assertEqual(built["reference_rvol"]["status"]["code"], "insufficient_history")
        self.assertEqual(built["reference_momentum"]["status"]["code"], "warming_up")
        self.assertIsNone(built["reference_rvol"]["values"]["rvol_multiple"])

    def test_partial_session_travels_with_vwap_coverage(self):
        market = market_block()
        market["vwap_partial_session"] = True
        reading = next(r for r in build(market=market) if r["reading_id"] == "reference_vwap")
        self.assertTrue(reading["coverage"]["partial"])
        self.assertEqual(reading["coverage"]["reason"], "partial_session")

    def test_zero_and_missing_open_interest_are_reported_alike(self):
        zero = [row(600, "call", 0), row(600, "put", 0)]
        missing = [row(600, "call", None), row(600, "put", None)]
        self.assertEqual(build(rows=zero), build(rows=missing))
        pcr = compute_reference_pcr(zero)
        self.assertEqual(pcr["status"]["code"], "undefined")
        self.assertIn("oi_zero_or_missing_indistinguishable",
                      next(r for r in build(rows=zero) if r["reading_id"] == "reference_pcr")["caveats"])


class ImmutableJoin(unittest.TestCase):
    def payload(self, readings=None, digest=None):
        return {
            "timestamp": NOW.isoformat(), "snapshot_time": "12:00 ET",
            "expiration": EXP, "underlying_price": 600.1, "rows": chain(),
            "snapshot_key": KEY,
            "market_readings": readings if readings is not None else build(),
            "market_readings_digest": digest if digest is not None else readings_digest(build()),
        }

    def test_crassus_recomputes_the_same_digest_across_the_boundary(self):
        built = build()
        digest = readings_digest(built)
        snapshot = MarketSnapshot.from_payload("test://board", self.payload(built, digest), b"{}")
        self.assertEqual(snapshot.readings_digest, digest)
        self.assertEqual(snapshot.readings_status, "valid")
        join = snapshot.readings_join
        self.assertEqual(join["snapshot_key"], KEY)
        self.assertEqual(join["readings_digest"], digest)
        self.assertEqual(join["reading_ids"], [r["reading_id"] for r in built])

    def test_tampered_digest_is_exposed_not_trusted(self):
        snapshot = MarketSnapshot.from_payload(
            "test://board", self.payload(build(), "0" * 64), b"{}")
        self.assertEqual(snapshot.readings_status, "digest_mismatch")

    def test_malformed_reading_elements_are_invalid_not_a_crash(self):
        good = build()[0]
        for malformed in ([good, "not-a-dict"], [None], [1, 2], {"reading_id": "x"}):
            snapshot = MarketSnapshot.from_payload(
                "test://board", self.payload(malformed, "ignored"), b"{}")
            self.assertEqual(snapshot.readings_status, "invalid")
            self.assertIsNone(snapshot.readings_digest)
            self.assertEqual(snapshot.readings_join["status"], "invalid")
            self.assertEqual(snapshot.readings_join["reading_ids"], [])

    def test_absent_readings_keep_the_legacy_absent_behavior(self):
        payload = {k: v for k, v in self.payload().items()
                   if k not in ("market_readings", "market_readings_digest")}
        snapshot = MarketSnapshot.from_payload("test://board", payload, b"{}")
        self.assertEqual(snapshot.readings_status, "absent")
        self.assertIsNone(snapshot.readings_digest)
        self.assertEqual(snapshot.readings_join["status"], "absent")
        self.assertEqual(snapshot.readings_join["reading_ids"], [])

    def test_join_filters_unchecked_elements_even_on_direct_construction(self):
        built = build()
        snapshot = MarketSnapshot.from_payload(
            "test://board", self.payload(built, readings_digest(built)), b"{}")
        object.__setattr__(snapshot, "readings", (built[0], "malformed", None))
        join = snapshot.readings_join
        self.assertEqual(join["reading_ids"], [built[0]["reading_id"]])

    def test_decision_base_carries_the_join_and_no_snapshot_is_explicit(self):
        built = build()
        snapshot = MarketSnapshot.from_payload(
            "test://board", self.payload(built, readings_digest(built)), b"{}")
        base = decision_base(decision_id="d1", alias="A", strategy_id="s",
                             strategy_version="1", snapshot=snapshot)
        self.assertEqual(base["market_readings_join"], snapshot.readings_join)
        empty = decision_base(decision_id="d2", alias="A", strategy_id="s",
                              strategy_version="1", snapshot=None)
        self.assertEqual(empty["market_readings_join"]["status"], "unavailable")
        self.assertEqual(empty["market_readings_join"]["reading_ids"], [])

    def test_account_parameters_stay_separate_from_the_reference_band(self):
        wide = []
        for strike in (580, 590, 595, 600, 605, 610, 620):
            edge = strike in (580, 620)
            wide.append(row(strike, "call", 900 if edge else 100))
            wide.append(row(strike, "put", 10 if edge else 80))
        built = {r["reading_id"]: r for r in build(rows=wide)}
        reference_band = built["reference_oi_skew"]["values"]["imbalance_ratio"]
        snapshot = MarketSnapshot.from_payload(
            "test://board",
            {"timestamp": NOW.isoformat(), "snapshot_time": "12:00 ET",
             "expiration": EXP, "underlying_price": 600.1, "rows": wide,
             "snapshot_key": KEY, "market_readings": [],
             "market_readings_digest": readings_digest([])}, b"{}")
        ctx = StrategyContext(
            snapshot=snapshot, account_state={"username": "acct"}, book=Book([]),
            now_et=NOW, session_phase="open", quotes=lambda syms: {},
            params={"near_money_pct": 0.04},
        )
        decision = oi_skew._decide(ctx)
        self.assertEqual(built["reference_oi_skew"]["calculation"]["parameters"]["near_money_pct"], 0.02)
        self.assertNotEqual(decision.metadata["imbalance_ratio"], reference_band)


class NoBotNeeded(unittest.TestCase):
    def test_reading_module_does_not_import_the_strategy_runtime(self):
        code = (
            "import sys; import market_data.overview.readings; "
            "print('crassus' in sys.modules or 'crassus.strategy' in sys.modules)"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


class FakeBody:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw


class FakeS3:
    def __init__(self):
        self.store = {}

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": FakeBody(self.store[Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803
        self.store[Key] = Body if isinstance(Body, bytes) else str(Body).encode()

    def json(self, key):
        return json.loads(self.store[key].decode())


class FakeFeed:
    def __init__(self, state):
        self.state = state

    def get_state(self):
        return {k: dict(v) for k, v in self.state.items()}


def strike(symbol, strike_price):
    k = f"{int(strike_price * 1000):08d}"
    return {
        "strike": float(strike_price),
        "call_sym": f".{symbol}{TODAY.strftime('%y%m%d')}C{int(strike_price)}",
        "put_sym": f".{symbol}{TODAY.strftime('%y%m%d')}P{int(strike_price)}",
        "call_occ": f"{symbol:<6}{TODAY.strftime('%y%m%d')}C{k}",
        "put_occ": f"{symbol:<6}{TODAY.strftime('%y%m%d')}P{k}",
    }


class PublishedWithTheSnapshot(unittest.TestCase):
    def test_readings_and_join_digest_publish_without_any_bot(self):
        s3 = FakeS3()
        # The contract symbols must follow the same clock as strike(): after
        # 00:00Z a hardcoded YYMMDD no longer matches TODAY and the snapshot is
        # (correctly) archived without option quotes.
        option_date = TODAY.strftime("%y%m%d")
        state = {
            "QQQ": {"bid": 600.0, "ask": 600.2,
                    "bid_ts": (NOW - timedelta(seconds=2)).isoformat(),
                    "ask_ts": (NOW - timedelta(seconds=1)).isoformat(), "volume": 1000},
            f".QQQ{option_date}C600": {"bid": 1.0, "ask": 1.2, "oi": 100, "volume": 5},
            f".QQQ{option_date}P600": {"bid": 1.1, "ask": 1.3, "oi": 300, "volume": 5},
        }
        us = collector.UnderlyingSession("QQQ")
        collector.take_snapshot(s3, FakeFeed(state), [strike("QQQ", 600)], EXP,
                                "0DTE_Regular", TODAY, collector.Counters(),
                                collector.SnapshotTracker(), us)

        latest = s3.json(us.latest_key)
        csv_key = latest["snapshot_key"]
        sidecar = s3.json(csv_key[:-4] + ".provenance.json")
        ids = [r["reading_id"] for r in latest["market_readings"]]
        self.assertEqual(ids, ["reference_vwap", "reference_rvol", "reference_momentum",
                               "reference_pcr", "reference_max_pain", "reference_oi_skew", "qqq_iv_header"])
        self.assertEqual(latest["market_readings_digest"], readings_digest(latest["market_readings"]))
        self.assertEqual(sidecar["market_readings_digest"], latest["market_readings_digest"])
        self.assertIn("market", sidecar)
        self.assertTrue(all(r["publication"]["status"] == "published"
                            for r in latest["market_readings"]))

        snapshot = MarketSnapshot.from_payload(us.latest_key, latest, b"{}")
        self.assertEqual(snapshot.readings_digest, latest["market_readings_digest"])


if __name__ == "__main__":
    unittest.main()
