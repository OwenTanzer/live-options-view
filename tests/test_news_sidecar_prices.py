"""Last-trade sidecar adapter: collector provenance sidecars -> underlying_quote records.

Base fixture: the REAL public sidecar intraday/20261113/snapshot_161416905398.provenance.json
(a midpoint selection). Last-trade cases put the collector's OWN selection output
(market_data.live_market.market._select_underlying_quote with no two-sided quote)
into that sidecar, so the mapping is checked against the producer's code path,
not a hand-written shape. The exchange calendar is a fixed trading-day set (no network).
"""
import copy
import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from test_news_support import FIXTURES, ROOT
from market_data.news.adapters import sidecar_prices as sc
from market_data.news.join import PriceSeries, match_price
from market_data.news.prices import load_price_file, parse_record

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from market_data.live_market import price_sessions as ps
    from market_data.live_market.market import _select_underlying_quote

SIDECAR_DIR = FIXTURES / "sidecars"
KEY = "intraday/20261113/snapshot_161416905398.provenance.json"
REAL = json.loads((SIDECAR_DIR / KEY).read_bytes())

# NYSE trading days Nov 2026 (Thanksgiving 11-26 closed; 11-27 early close 13:00 ET)
TRADING = {date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 23, 24, 25, 27, 30)}


def deriver():
    return sc.SessionDeriver(lambda et: ps.EquityCalendar(TRADING, basis="test_fixed_days"))


def et_utc(y, mo, d, h, mi, s=0):
    return ps.ET.localize(datetime(y, mo, d, h, mi, s)).astimezone(timezone.utc)


def last_trade_sidecar(observed: datetime, receipt: datetime, archive: datetime, price=679.9,
                       last_session=None, key=KEY):
    """The real sidecar with the collector's own last-trade selection in place of its midpoint."""
    sel = _select_underlying_quote({"bid": None, "ask": None, "last": price,
                                    "last_ts": receipt.isoformat(), "last_observed_at": observed.isoformat(),
                                    "last_session": last_session}, None, None)
    d = copy.deepcopy(REAL)
    d["underlying"] = sel.as_provenance()
    d["archive_uploaded_at"] = archive.isoformat()
    d["snapshot_key"] = key[: -len(sc.SIDECAR_SUFFIX)] + ".csv"
    return d


# The real fixture's own last-trade times (20:59:59.942Z regular-session trade, received 21:14:16.801618Z)
OBS = datetime(2026, 11, 13, 20, 59, 59, 942000, tzinfo=timezone.utc)
RCV = datetime(2026, 11, 13, 21, 14, 16, 801618, tzinfo=timezone.utc)
ARC = datetime(2026, 11, 13, 21, 14, 18, 730925, tzinfo=timezone.utc)


def one(sidecar, key=KEY):
    return sc.map_sidecar(key, sidecar, deriver())


class RealSidecar(unittest.TestCase):
    def test_real_fixture_is_a_midpoint_and_is_excluded(self):
        self.assertEqual(REAL["underlying"]["price_basis"], "bid_ask_midpoint")
        self.assertIsNone(REAL["underlying"]["provider_observed_at"])
        rec, why = one(REAL)
        self.assertIsNone(rec)
        self.assertEqual(why, "excluded_basis:bid_ask_midpoint")

    def test_real_midpoint_keeps_last_trade_times_but_not_its_price(self):
        # the finding behind the yield note: contributing has last_ts/last_observed_at, no last price
        c = REAL["underlying"]["contributing"]
        self.assertEqual(c["last_observed_at"], "2026-11-13T20:59:59.942000+00:00")
        self.assertNotIn("last", c)
        self.assertNotIn("last_price", c)


class Mapping(unittest.TestCase):
    def test_collector_last_trade_maps_with_three_separate_times(self):
        rec, why = one(last_trade_sidecar(OBS, RCV, ARC))
        self.assertIsNone(why)
        self.assertEqual(rec["record_kind"], "underlying_quote")
        self.assertEqual(rec["instrument_id"], "etf.qqq")
        self.assertEqual(rec["price"], 679.9)
        self.assertEqual(rec["price_basis"], "last_trade")
        self.assertEqual(rec["quote_ts"], "2026-11-13T20:59:59.942000Z")
        self.assertEqual(rec["collector_received_at"], "2026-11-13T21:14:16.801618Z")
        self.assertEqual(rec["available_at"], rec["collector_received_at"])
        self.assertEqual(rec["archive_available_at"], "2026-11-13T21:14:18.730925Z")
        self.assertEqual(rec["session"], "regular")
        self.assertEqual(rec["session_basis"], "exchange_calendar_at_provider_time")
        self.assertEqual(rec["record_id"], "sc-20261113-161416905398")
        self.assertEqual(rec["source_object"], KEY)
        self.assertFalse(rec["synthetic"])
        parsed = parse_record(rec)  # satisfies the unchanged prices.py contract
        self.assertEqual(parsed.price_time, OBS)
        self.assertEqual(parsed.available_at, RCV)

    def test_each_time_comes_only_from_its_own_field(self):
        base, _ = one(last_trade_sidecar(OBS, RCV, ARC))
        later_archive, _ = one(last_trade_sidecar(OBS, RCV, ARC + timedelta(minutes=7)))
        self.assertEqual({k for k in base if base[k] != later_archive[k]}, {"archive_available_at"})
        later_receipt, _ = one(last_trade_sidecar(OBS, RCV + timedelta(seconds=30), ARC + timedelta(minutes=1)))
        self.assertEqual(later_receipt["quote_ts"], base["quote_ts"])
        self.assertNotEqual(later_receipt["available_at"], base["available_at"])

    def test_offsets_are_normalized_to_utc(self):
        d = last_trade_sidecar(OBS, RCV, ARC)
        d["underlying"]["provider_observed_at"] = "2026-11-13T15:59:59.942000-05:00"
        d["underlying"]["contributing"]["last_observed_at"] = "2026-11-13T15:59:59.942000-05:00"
        rec, why = one(d)
        self.assertIsNone(why)
        self.assertEqual(rec["quote_ts"], "2026-11-13T20:59:59.942000Z")


class Rejections(unittest.TestCase):
    def assertRejected(self, sidecar, reason, key=KEY):
        rec, why = one(sidecar, key)
        self.assertIsNone(rec)
        self.assertEqual(why, reason)

    def test_missing_provenance_is_rejected_never_filled(self):
        d = last_trade_sidecar(OBS, RCV, ARC); d["underlying"]["provider_observed_at"] = None
        self.assertRejected(d, "missing_provider_time")
        d = last_trade_sidecar(OBS, RCV, ARC); d["underlying"]["collector_received_at"] = None
        self.assertRejected(d, "missing_collector_receipt")
        d = last_trade_sidecar(OBS, RCV, ARC); del d["archive_uploaded_at"]
        self.assertRejected(d, "missing_archive_upload_time")

    def test_collector_last_trade_without_provider_time_is_rejected(self):
        sel = _select_underlying_quote({"bid": None, "ask": None, "last": 679.9, "last_ts": RCV.isoformat(),
                                        "last_observed_at": None}, None, None)
        d = copy.deepcopy(REAL); d["underlying"] = sel.as_provenance()
        self.assertRejected(d, "missing_provider_time")

    def test_naive_or_bad_times_are_rejected(self):
        d = last_trade_sidecar(OBS, RCV, ARC); d["archive_uploaded_at"] = "2026-11-13T21:14:18"
        self.assertRejected(d, "time_unparseable_or_without_offset")
        d = last_trade_sidecar(OBS, RCV, ARC); d["underlying"]["collector_received_at"] = "yesterday"
        self.assertRejected(d, "time_unparseable_or_without_offset")

    def test_selected_times_must_be_the_last_trades_own(self):
        d = last_trade_sidecar(OBS, RCV, ARC); d["underlying"]["contributing"]["last_observed_at"] = None
        self.assertRejected(d, "provider_time_not_last_trade")
        d = last_trade_sidecar(OBS, RCV, ARC); d["underlying"]["contributing"]["last_ts"] = ARC.isoformat()
        self.assertRejected(d, "receipt_not_last_trade")

    def test_time_order(self):
        self.assertRejected(last_trade_sidecar(RCV, OBS, ARC), "receipt_before_provider_time")
        self.assertRejected(last_trade_sidecar(OBS, RCV, RCV - timedelta(seconds=1)), "archive_upload_before_receipt")

    def test_other_bases_are_excluded(self):
        for qqq, reason in (
            ({"bid": 1.0, "ask": 2.0, "bid_ts": None, "ask_ts": None}, "excluded_basis:bid_ask_midpoint_unstamped"),
            ({"bid": None, "ask": None, "last": None}, "underlying_unavailable"),
        ):
            d = copy.deepcopy(REAL); d["underlying"] = _select_underlying_quote(qqq, None, None).as_provenance()
            self.assertRejected(d, reason)
        d = copy.deepcopy(REAL)
        d["underlying"] = _select_underlying_quote({"bid": None, "ask": None, "last": None}, 600.0,
                                                   RCV.isoformat()).as_provenance()
        self.assertRejected(d, "excluded_basis:remembered_spot")
        d = last_trade_sidecar(OBS, RCV, ARC); d["underlying"]["selection_rule"] = "carried_forward_spot"
        self.assertRejected(d, "selection_rule_not_last_trade")

    def test_price_must_be_finite_positive(self):
        for bad in (math.nan, math.inf, 0, -1, True, "679.9"):
            d = last_trade_sidecar(OBS, RCV, ARC); d["underlying"]["price"] = bad
            self.assertRejected(d, "price_not_finite_positive")

    def test_identity_checks(self):
        self.assertRejected(last_trade_sidecar(OBS, RCV, ARC), "key_not_a_sidecar",
                            key="intraday/20261113/snapshot_161416905398.csv")
        self.assertRejected(last_trade_sidecar(OBS, RCV, ARC), "snapshot_key_mismatch",
                            key="intraday/20261113/snapshot_999.provenance.json")
        d = last_trade_sidecar(OBS, RCV, ARC); d["symbol"] = "SPY"
        self.assertRejected(d, "symbol_not_qqq")
        d = last_trade_sidecar(OBS, RCV, ARC); d["schema_version"] = "snapshot_provenance.v2"
        self.assertRejected(d, "schema_not_snapshot_provenance_v1")


class Sessions(unittest.TestCase):
    def session(self, observed, last_session=None):
        receipt = observed + timedelta(seconds=1)
        return one(last_trade_sidecar(observed, receipt, receipt + timedelta(seconds=2), last_session=last_session))

    def test_session_from_exchange_calendar(self):
        cases = [
            (et_utc(2026, 11, 13, 4, 0), "pre"), (et_utc(2026, 11, 13, 9, 29, 59), "pre"),
            (et_utc(2026, 11, 13, 9, 30), "regular"), (et_utc(2026, 11, 13, 15, 59, 59), "regular"),
            (et_utc(2026, 11, 13, 16, 0), "post"), (et_utc(2026, 11, 13, 19, 59, 59), "post"),
            (et_utc(2026, 11, 27, 12, 59), "regular"), (et_utc(2026, 11, 27, 13, 0), "post"),  # early close
        ]
        for t, want in cases:
            rec, why = self.session(t)
            self.assertIsNone(why, t)
            self.assertEqual(rec["session"], want, t)

    def test_outside_sessions_are_rejected(self):
        for t in (et_utc(2026, 11, 13, 3, 59), et_utc(2026, 11, 13, 20, 0),
                  et_utc(2026, 11, 14, 11, 0), et_utc(2026, 11, 26, 11, 0)):  # pre-04:00, post-20:00, Saturday, Thanksgiving
            rec, why = self.session(t)
            self.assertEqual(why, "outside_exchange_session", t)

    def test_calendar_failure_fails_closed(self):
        def broken(et):
            raise RuntimeError("calendar down")
        d = last_trade_sidecar(OBS, RCV, ARC)
        rec, why = sc.map_sidecar(KEY, d, sc.SessionDeriver(broken))
        self.assertEqual((rec, why), (None, "calendar_unavailable"))

    def test_collector_session_must_agree(self):
        self.assertEqual(self.session(et_utc(2026, 11, 13, 8, 0), "regular")[1], "session_conflict")
        self.assertEqual(self.session(et_utc(2026, 11, 13, 8, 0), "extended")[0]["session"], "pre")
        self.assertEqual(self.session(et_utc(2026, 11, 13, 10, 0), "regular")[0]["source_session"], "regular")
        self.assertEqual(self.session(et_utc(2026, 11, 13, 10, 0), "mystery")[1], "session_conflict")


class AdaptAndWrite(unittest.TestCase):
    def items(self):
        k2 = "intraday/20261113/snapshot_101500000001.provenance.json"
        k3 = "intraday/20261113/snapshot_101400000001.provenance.json"
        t2, t3 = et_utc(2026, 11, 13, 10, 14, 59), et_utc(2026, 11, 13, 10, 13, 59)
        return [
            (KEY, json.dumps(REAL).encode()),
            (k2, json.dumps(last_trade_sidecar(t2, t2 + timedelta(seconds=1), t2 + timedelta(seconds=3), 601.5, key=k2)).encode()),
            (k3, json.dumps(last_trade_sidecar(t3, t3 + timedelta(seconds=1), t3 + timedelta(seconds=3), 601.0, key=k3)).encode()),
            ("intraday/20261113/snapshot_5.provenance.json", b"{not json"),
        ]

    def test_counts_order_and_contract_round_trip(self):
        result = sc.adapt(self.items(), deriver())
        self.assertEqual(result.sidecars_read, 4)
        self.assertEqual(dict(result.rejected), {"excluded_basis:bid_ask_midpoint": 1, "not_json": 1})
        self.assertEqual([r["price"] for r in result.records], [601.0, 601.5])  # sorted by availability
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "prices.jsonl"
            sc.write_jsonl(result, out, "test")
            raw = out.read_bytes()
            self.assertNotIn(b"\r\n", raw)
            pf = load_price_file(out)
        self.assertFalse(pf.header["synthetic"])
        self.assertEqual(pf.header["rejected"], {"excluded_basis:bid_ask_midpoint": 1, "not_json": 1})
        self.assertEqual(len(pf.records), 2)
        # the join sees a record only from its receipt, and ages it by the provider time
        series = PriceSeries(pf.records, "etf.qqq", "underlying_quote")
        t3 = et_utc(2026, 11, 13, 10, 13, 59)
        before = match_price(series, t3 + timedelta(milliseconds=500), t3 + timedelta(hours=1), timedelta(seconds=120))
        self.assertNotEqual(before["status"], "matched")
        after = match_price(series, t3 + timedelta(seconds=30), t3 + timedelta(hours=1), timedelta(seconds=120))
        self.assertEqual(after["status"], "matched")

    def test_duplicate_record_ids_are_rejected(self):
        d = json.dumps(last_trade_sidecar(OBS, RCV, ARC)).encode()
        result = sc.adapt([(KEY, d), (KEY, d)], deriver())
        self.assertEqual((len(result.records), dict(result.rejected)), (1, {"duplicate_record_id": 1}))


class FakeClient:
    def __init__(self, keys):
        self.keys, self.calls = keys, []

    def get_paginator(self, name):
        self.calls.append(("paginator", name))
        client = self

        class P:
            def paginate(self, **kw):
                client.calls.append(("list", kw))
                return [{"Contents": [{"Key": k} for k in client.keys]}]
        return P()

    def get_object(self, **kw):
        self.calls.append(("get", kw))
        return {"Body": io.BytesIO(json.dumps(REAL).encode())}

    def __getattr__(self, name):  # any other call (put/delete/copy/...) is a test failure
        raise AssertionError(f"unexpected client call {name}")


class ReadOnlyAccess(unittest.TestCase):
    def test_prefix_guard(self):
        self.assertEqual(sc.check_prefix("intraday/20261113/"), "intraday/20261113/")
        for bad in ("intraday/", "", "news/v1/", "intraday/2026111/", "intraday/20261113/../",
                    "intraday/20261113", "/intraday/20261113/", "prices/", None):
            with self.assertRaises(sc.SidecarAccessError, msg=bad):
                sc.check_prefix(bad)

    def test_bucket_lists_and_gets_only_under_the_day_prefix(self):
        client = FakeClient([KEY, "intraday/20261113/snapshot_161416905398.csv",
                             "intraday/20261113/latest.json", "intraday/20261114/snapshot_1.provenance.json"])
        got = list(sc.BucketSidecars(client, "b").sidecars("20261113"))
        self.assertEqual([k for k, _ in got], [KEY])
        self.assertEqual(client.calls, [("paginator", "list_objects_v2"),
                                        ("list", {"Bucket": "b", "Prefix": "intraday/20261113/"}),
                                        ("get", {"Bucket": "b", "Key": KEY})])

    def test_bucket_refuses_other_keys_and_prefixes(self):
        b = sc.BucketSidecars(FakeClient([]), "b")
        for key in ("news/v1/publish/head.json", "intraday/latest.json", "intraday/20261113/snapshot_1.csv"):
            with self.assertRaises(sc.SidecarAccessError):
                b.get(key)
        with self.assertRaises(sc.SidecarAccessError):
            list(b.sidecars("2026111"))

    def test_from_env_needs_all_read_credentials(self):
        with self.assertRaises(sc.SidecarAccessError) as cm:
            sc.BucketSidecars.from_env({"PRICE_SIDECAR_R2_BUCKET_NAME": "x"})
        self.assertIn("PRICE_SIDECAR_R2_ACCESS_KEY_ID", str(cm.exception))


class Cli(unittest.TestCase):
    def run_cli(self, *args, env=None):
        import os
        e = {k: v for k, v in os.environ.items() if not k.startswith("PRICE_SIDECAR_R2_")}
        e["PYTHONWARNINGS"] = "ignore"
        return subprocess.run([sys.executable, "-m", "market_data.news", "prices-from-sidecars", *args],
                              cwd=ROOT, capture_output=True, text=True, env=e)

    def test_local_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "p.jsonl"
            r = self.run_cli("--dir", str(SIDECAR_DIR), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            header = json.loads(r.stdout)
            self.assertEqual((header["sidecars_read"], header["records"]), (1, 0))
            self.assertEqual(header["rejected"], {"excluded_basis:bid_ask_midpoint": 1})
            self.assertEqual(load_price_file(out).records, [])

    def test_bucket_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "p.jsonl"
            r = self.run_cli("--date", "../news", "--out", str(out))
            self.assertEqual(r.returncode, 2)
            self.assertIn("refused prefix", r.stderr)
            r = self.run_cli("--date", "20261113", "--out", str(out))
            self.assertEqual(r.returncode, 2)
            self.assertIn("missing PRICE_SIDECAR_R2_", r.stderr)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
