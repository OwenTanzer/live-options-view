"""Offline producer tests for snapshot price provenance (#146).

No credentials, sockets or provider calls: a fake feed, a fake object store and
a fixed clock. These prove the selected underlying value carries its own
source, basis, receipt time and provider event time into the durable snapshot
artifacts, and that a fresh half-quote cannot rejuvenate an older one.

    python -m unittest discover -s tests -p test_snapshot_provenance.py -v
"""

import csv
import dataclasses
import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "research" / "synthetic_days"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import market_data.live_market as collector  # noqa: E402

from market_data.live_market import market  # noqa: E402
from market_data.live_market.market import _select_underlying_quote  # noqa: E402

UTC = timezone.utc
NOW = datetime.now(UTC)
OLD = (NOW - timedelta(minutes=10)).isoformat()
FRESH = (NOW - timedelta(seconds=5)).isoformat()
TODAY = NOW.date()
EXP = TODAY.isoformat()


class FakeBody:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self):
        return self.raw


class FakeS3:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.store:
            raise RuntimeError(f"missing key: {Key}")
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


def strike(root, price):
    k = f"{int(price * 1000):08d}"
    yymmdd = "261109"
    return {
        "strike": float(price),
        "call_sym": f".{root}{yymmdd}C{int(price)}",
        "put_sym": f".{root}{yymmdd}P{int(price)}",
        "call_occ": f"{root:<6}{yymmdd}C{k}",
        "put_occ": f"{root:<6}{yymmdd}P{k}",
    }


QQQ_STRIKES = [strike("QQQ", 600)]


def state_for(qqq: dict, option_quote=(1.0, 1.2)):
    bid, ask = option_quote
    return {
        "QQQ": qqq,
        ".QQQ261109C600": {"bid": bid, "ask": ask, "oi": 10, "volume": 5},
        ".QQQ261109P600": {"bid": bid, "ask": ask, "oi": 11, "volume": 5},
    }


def snapshot(s3, qqq, *, us=None, strikes=None, option_quote=(1.0, 1.2)):
    us = us or collector.UnderlyingSession("QQQ")
    collector.take_snapshot(
        s3, FakeFeed(state_for(qqq, option_quote)), strikes or QQQ_STRIKES,
        EXP, "0DTE_Regular", TODAY, collector.Counters(),
        collector.SnapshotTracker(), us,
    )
    return us


def latest_key(us):
    return us.latest_key


def sidecar_key(csv_key):
    return csv_key[:-4] + ".provenance.json"


class SelectionContract(unittest.TestCase):
    def test_midpoint_uses_older_contributing_side(self):
        selection = _select_underlying_quote(
            {"bid": 600.0, "ask": 600.2, "bid_ts": OLD, "ask_ts": FRESH,
             "bid_observed_at": OLD, "ask_observed_at": FRESH}, None, None)
        self.assertEqual(selection.price, 600.1)
        self.assertEqual(selection.basis, "bid_ask_midpoint")
        self.assertEqual(selection.receipt_ts, OLD)
        self.assertEqual(selection.provider_observed_at, OLD)
        self.assertEqual(selection.contributing["midpoint_rejected"], None)

    def test_missing_side_receipt_is_explicit_and_unstamped(self):
        selection = _select_underlying_quote(
            {"bid": 600.0, "ask": 600.2, "ask_ts": FRESH}, None, None)
        self.assertEqual(selection.price, 600.1)
        self.assertEqual(selection.basis, "bid_ask_midpoint_unstamped")
        self.assertIsNone(selection.receipt_ts)
        self.assertEqual(selection.contributing["midpoint_rejected"], "missing_side_receipt")

    def test_missing_provider_event_time_stays_unknown(self):
        selection = _select_underlying_quote(
            {"bid": 600.0, "ask": 600.2, "bid_ts": OLD, "ask_ts": FRESH,
             "bid_observed_at": OLD}, None, None)
        self.assertIsNone(selection.provider_observed_at)

    def test_fresh_ask_never_rejuvenates_a_stale_last_trade(self):
        selection = _select_underlying_quote(
            {"bid": 600.0, "ask": 600.2, "ask_ts": FRESH,
             "last": 599.5, "last_ts": OLD, "last_observed_at": OLD}, None, None)
        # The midpoint exists but cannot meet the two-sided receipt contract,
        # so it is kept unstamped; the last trade is not preferred just
        # because its own receipt is older.
        self.assertEqual(selection.basis, "bid_ask_midpoint_unstamped")
        self.assertIsNone(selection.receipt_ts)

    def test_last_trade_carries_its_own_provider_time(self):
        selection = _select_underlying_quote(
            {"last": 599.5, "last_ts": OLD, "last_observed_at": OLD,
             "last_session": "regular"}, None, None)
        self.assertEqual((selection.price, selection.receipt_ts, selection.basis),
                         (599.5, OLD, "last_trade"))
        self.assertEqual(selection.provider_observed_at, OLD)

    def test_remembered_spot_never_borrows_a_feed_time(self):
        selection = _select_underlying_quote({}, 405.25, None)
        self.assertEqual((selection.price, selection.receipt_ts, selection.basis),
                         (405.25, None, "remembered_spot"))
        self.assertIsNone(selection.provider_observed_at)


class SavedArtifacts(unittest.TestCase):
    def test_csv_and_sidecar_round_trip_with_qqq_and_spy(self):
        s3 = FakeS3()
        us = snapshot(s3, {"bid": 600.0, "ask": 600.2, "bid_ts": OLD, "ask_ts": FRESH,
                           "volume": 1000})
        latest = s3.json(latest_key(us))
        csv_key = latest["snapshot_key"]
        provenance = s3.json(sidecar_key(csv_key))

        self.assertEqual(provenance["schema_version"], "snapshot_provenance.v1")
        self.assertEqual(provenance["snapshot_key"], csv_key)
        self.assertEqual(provenance["publication"]["status"], "published")
        self.assertIsNotNone(provenance["latest_published_at"])
        underlying = provenance["underlying"]
        self.assertEqual(underlying["price_basis"], "bid_ask_midpoint")
        self.assertEqual(underlying["collector_received_at"], OLD)
        self.assertEqual(underlying["freshness"], "stale")
        self.assertEqual(underlying["contributing"]["ask_ts"], FRESH)
        self.assertEqual(latest["underlying_market"]["provenance"], underlying)

        # The CSV keeps its pre-provenance schema and stays readable by a real
        # consumer that only knows the named columns.
        raw = s3.store[csv_key].decode()
        rows = list(csv.DictReader(io.StringIO(raw)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[0]["UnderlyingPrice"]), 600.1)
        self.assertNotIn("UnderlyingPriceBasis", rows[0])

        from r2_sources import R2Source
        with tempfile.TemporaryDirectory() as cache:
            source = R2Source(cache_dir=cache, s3=s3)
            consumer_rows = source.intraday_snapshot_csv(csv_key)
        self.assertEqual(float(consumer_rows[0]["UnderlyingPrice"]), 600.1)

    def test_spy_uses_the_same_older_receipt_contract(self):
        s3 = FakeS3()
        spy = collector.UnderlyingSession("SPY")
        state = {
            "SPY": {"bid": 670.0, "ask": 670.4, "bid_ts": OLD, "ask_ts": FRESH, "volume": 9},
            ".SPY261109C670": {"bid": 2.0, "ask": 2.2, "oi": 20, "volume": 5},
            ".SPY261109P670": {"bid": 2.1, "ask": 2.3, "oi": 21, "volume": 5},
        }
        collector.take_snapshot(
            s3, FakeFeed(state), [strike("SPY", 670)], EXP, "0DTE_Regular",
            TODAY, collector.Counters(), collector.SnapshotTracker(), spy)
        latest = s3.json(spy.latest_key)
        provenance = s3.json(sidecar_key(latest["snapshot_key"]))
        self.assertEqual(provenance["underlying"]["price_basis"], "bid_ask_midpoint")
        self.assertEqual(provenance["underlying"]["collector_received_at"], OLD)
        self.assertEqual(provenance["underlying"]["freshness"], "stale")

    def test_archived_without_publication_records_the_reason(self):
        s3 = FakeS3()
        us = snapshot(s3, {"bid": 600.0, "ask": 600.2, "bid_ts": OLD, "ask_ts": FRESH},
                      option_quote=(None, None))
        self.assertNotIn(us.latest_key, s3.store)
        csv_keys = [k for k in s3.store if k.endswith(".csv") and "snapshot_" in k]
        self.assertTrue(csv_keys)
        provenance = s3.json(sidecar_key(csv_keys[0]))
        self.assertEqual(provenance["publication"]["status"], "archived_unpublished")
        self.assertEqual(provenance["publication"]["reason"], "no_option_quotes")
        self.assertIsNone(provenance["latest_published_at"])

    def test_restored_spot_stays_unknown_rather_than_live(self):
        s3 = FakeS3()
        us = collector.UnderlyingSession("QQQ")
        us.last_spot[:] = [605.5, None]
        us = snapshot(s3, {}, us=us)
        latest = s3.json(us.latest_key)
        provenance = latest["underlying_market"]["provenance"]
        self.assertEqual(provenance["price_basis"], "remembered_spot")
        self.assertIsNone(provenance["collector_received_at"])
        self.assertEqual(provenance["freshness"], "stale")


def _legacy_contributing(qqq: dict) -> dict:
    """`_quote_contributing` exactly as it was before `last_price` existed."""
    return {key: qqq.get(key) for key in (
        "bid_ts", "ask_ts", "last_ts", "bid_observed_at", "ask_observed_at", "last_observed_at")}


def _without_last_price(selection):
    contributing = dict(selection.contributing)
    contributing.pop("last_price", None)
    return dataclasses.replace(selection, contributing=contributing)


LAST_OBS = (NOW - timedelta(minutes=3)).isoformat()
LAST_RCV = (NOW - timedelta(minutes=3) + timedelta(milliseconds=40)).isoformat()
TRADE = {"last": 599.87, "last_ts": LAST_RCV, "last_observed_at": LAST_OBS,
         "last_session": "regular"}


class LastTradePriceEvidence(unittest.TestCase):
    """The last trade's own price is recorded beside its contributing times.

    Additive evidence only: the selected spot, its basis, receipt, provider
    time, session and rule, the CSV and settlement inputs are unchanged.
    """

    QUOTES = {
        "midpoint": {"bid": 600.02, "ask": 600.2, "bid_ts": OLD, "ask_ts": FRESH,
                     "bid_observed_at": OLD, "ask_observed_at": FRESH, **TRADE},
        "unstamped_midpoint": {"bid": 600.0, "ask": 600.2, "ask_ts": FRESH, **TRADE},
        "last_trade": dict(TRADE),
    }

    def test_every_basis_records_the_last_trade_price(self):
        for name, quote in self.QUOTES.items():
            with self.subTest(name):
                c = _select_underlying_quote(quote, None, None).contributing
                self.assertEqual(c["last_price"], 599.87)
                self.assertEqual((c["last_ts"], c["last_observed_at"]), (LAST_RCV, LAST_OBS))

    def test_midpoint_selection_is_unchanged(self):
        selection = _select_underlying_quote(self.QUOTES["midpoint"], None, None)
        self.assertEqual((selection.price, selection.basis, selection.receipt_ts,
                          selection.provider_observed_at, selection.session,
                          selection.selection_rule),
                         (600.11, "bid_ask_midpoint", OLD, OLD, None,
                          "both_sides_required_older_receipt"))

    def test_selection_matches_the_pre_change_record_except_the_new_field(self):
        cases = dict(self.QUOTES, remembered={}, unavailable={})
        for name, quote in cases.items():
            with self.subTest(name):
                spot = (605.5, OLD) if name == "remembered" else (None, None)
                new = _select_underlying_quote(quote, *spot)
                with mock.patch.object(market, "_quote_contributing", _legacy_contributing):
                    old = _select_underlying_quote(quote, *spot)
                self.assertEqual(_without_last_price(new), old)
                self.assertEqual(set(new.contributing) - set(old.contributing), {"last_price"})

    def test_missing_or_unusable_price_stays_unknown(self):
        for value in (None, float("nan"), float("inf"), -float("inf"), "599.87", True):
            with self.subTest(value=value):
                quote = dict(self.QUOTES["midpoint"], last=value)
                self.assertIsNone(_select_underlying_quote(quote, None, None)
                                  .contributing["last_price"])
        no_trade = {k: v for k, v in self.QUOTES["midpoint"].items() if not k.startswith("last")}
        c = _select_underlying_quote(no_trade, None, None).contributing
        self.assertEqual((c["last_price"], c["last_ts"], c["last_observed_at"]), (None, None, None))

    def test_integer_price_is_recorded_as_a_number(self):
        quote = dict(self.QUOTES["midpoint"], last=600)
        price = _select_underlying_quote(quote, None, None).contributing["last_price"]
        self.assertEqual(price, 600.0)
        self.assertIsInstance(price, float)

    def test_midpoint_sidecar_carries_a_complete_last_trade_record(self):
        s3 = FakeS3()
        us = snapshot(s3, dict(self.QUOTES["midpoint"], volume=1000))
        latest = s3.json(us.latest_key)
        provenance = s3.json(sidecar_key(latest["snapshot_key"]))
        u = provenance["underlying"]
        self.assertEqual((u["price"], u["price_basis"], u["collector_received_at"]),
                         (600.11, "bid_ask_midpoint", OLD))
        self.assertEqual((u["contributing"]["last_price"], u["contributing"]["last_ts"],
                          u["contributing"]["last_observed_at"]), (599.87, LAST_RCV, LAST_OBS))
        self.assertEqual(latest["underlying_market"]["provenance"], u)
        rows = list(csv.DictReader(io.StringIO(s3.store[latest["snapshot_key"]].decode())))
        self.assertEqual(float(rows[0]["UnderlyingPrice"]), 600.11)

    def test_non_finite_state_price_never_costs_the_sidecar(self):
        # The sidecar is written with allow_nan=False; a NaN last price
        # restored into state must be recorded as unknown, not drop the record.
        s3 = FakeS3()
        us = snapshot(s3, dict(self.QUOTES["midpoint"], last=float("nan"), volume=1000))
        latest = s3.json(us.latest_key)
        provenance = s3.json(sidecar_key(latest["snapshot_key"]))
        self.assertIsNone(provenance["underlying"]["contributing"]["last_price"])
        self.assertEqual(provenance["underlying"]["price"], 600.11)

    def test_spy_sidecar_records_it_and_settlement_inputs_are_unchanged(self):
        state = {
            "SPY": {"bid": 670.0, "ask": 670.4, "bid_ts": OLD, "ask_ts": FRESH,
                    "bid_observed_at": OLD, "ask_observed_at": FRESH, "volume": 9,
                    "last": 670.1, "last_ts": LAST_RCV, "last_observed_at": LAST_OBS},
            ".SPY261109C670": {"bid": 2.0, "ask": 2.2, "oi": 20, "volume": 5},
            ".SPY261109P670": {"bid": 2.1, "ask": 2.3, "oi": 21, "volume": 5},
        }

        def run(contributing_fn):
            s3, calls = FakeS3(), []
            spy = collector.UnderlyingSession("SPY")
            with mock.patch.object(market, "_quote_contributing", contributing_fn), \
                 mock.patch("market_data.live_market.settlement_marks.publish_settlement_mark",
                            side_effect=lambda *a: calls.append(a[1:4] + a[4:5])):
                collector.take_snapshot(
                    s3, FakeFeed(state), [strike("SPY", 670)], EXP, "0DTE_Regular",
                    TODAY, collector.Counters(), collector.SnapshotTracker(), spy)
            latest = s3.json(spy.latest_key)
            return calls, s3.json(sidecar_key(latest["snapshot_key"]))["underlying"]

        new_calls, new_u = run(market._quote_contributing)
        old_calls, old_u = run(_legacy_contributing)
        self.assertEqual(new_u["contributing"]["last_price"], 670.1)
        self.assertEqual(new_calls, old_calls)
        self.assertTrue(new_calls)
        new_u["contributing"].pop("last_price")
        self.assertEqual(new_u, old_u)


if __name__ == "__main__":
    unittest.main()
