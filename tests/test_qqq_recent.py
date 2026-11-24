"""Exercise the archive-to-display boundary without contacting a provider."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("recent", ROOT/"market_data/tradier/recent.py")
recent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recent)


class RecentTests(unittest.TestCase):
    def test_signing_precedence_exclusions_and_snapshot(self):
        feed = recent.RecentPrints()
        symbol = "QQQ260925P00745000"
        ts = 1790343000102
        stamp = lambda t: datetime.fromtimestamp(t/1000, timezone.utc).isoformat()
        def trade(n, price, **extra):
            feed.observe(dict(type="timesale", symbol=symbol, seq=n, date=ts+n,
                              last=price, size=5, collector_receipt_timestamp=stamp(ts+n+10), **extra))
        trade(1, 2)
        feed.observe(dict(type="quote", symbol=symbol, bid=1.5, ask=1.7, biddate=ts-2,
                          askdate=ts-1, collector_receipt_timestamp=stamp(ts-1)))
        trade(2, 1.8)
        trade(3, 1.5)
        trade(3, 1.5)
        trade(4, 1.6, cancel=True)
        trade(2000, 1.5)
        self.assertEqual([p["side"] for p in feed.rows], ["unknown","buy","sell","sell"])
        self.assertTrue(all(p["size"]==5 and p["type"]=="P" and p["strike"]==745 for p in feed.rows))
        stop = Mock()
        stop.wait.side_effect = [False,False,True]
        client = Mock()
        client.put_object.side_effect = [OSError(),None]
        feed.publish(client,"bucket",stop,Event())
        saved = json.loads(client.put_object.call_args.kwargs["Body"])
        self.assertEqual(saved["rows"],list(feed.rows))
        self.assertEqual(saved["lastReceived"],ts+2000)

    def test_sequence_set_is_exact_out_of_order(self):
        seen = recent.SeqSet()
        self.assertEqual([seen.add(n) for n in (5, 3, 4, 6, 5, 3, 9, 7, 8, 2)],
                         [True, True, True, True, False, False, True, True, True, True])
        self.assertEqual((seen.low, seen.floor, seen.other, len(seen)), (5, 9, {2, 3, 4}, 8))


class SessionTests(unittest.TestCase):
    SYMBOL = "QQQ260925C00745000"
    T0 = 1790343000000

    def trade(self, feed, n, price, t=None, **extra):
        t = self.T0 + n if t is None else t
        feed.observe(dict(type="timesale", symbol=self.SYMBOL, seq=n, date=t, last=price, size=2,
                          collector_receipt_timestamp=datetime.fromtimestamp((t + 5) / 1000, timezone.utc).isoformat(),
                          **extra))

    def test_session_accumulates_beyond_display_cap_and_keeps_gaps(self):
        feed = recent.RecentPrints()
        for n in range(1, 3001):
            self.trade(feed, n, 1 + (n % 3) / 100)
        self.trade(feed, 7, 1.5)  # duplicate identity long after the old bounded horizon
        feed.observe(dict(type="gap", reason="stream_reconnect_resumed", outage_seconds=2.5,
                          receipt_timestamp=datetime.fromtimestamp((self.T0 + 9000) / 1000, timezone.utc).isoformat()))
        body = feed.payload()
        c = body["session"]["contracts"][self.SYMBOL]
        self.assertEqual(len(body["rows"]), 2048)
        self.assertEqual(body["session"]["prints"], 3000)
        self.assertEqual(c["B"] + c["S"] + c["U"], 6000)
        self.assertEqual(body["session"]["gaps"], [dict(start=self.T0 + 6500, end=self.T0 + 9000,
                                                         reason="stream_reconnect_resumed")])
        self.assertEqual(body["session"]["day"], "2026-09-25")

    def test_next_day_starts_a_new_session(self):
        feed = recent.RecentPrints()
        self.trade(feed, 1, 1.0)
        self.trade(feed, 1, 1.0, t=self.T0 + 86400000)
        self.assertEqual(feed.payload()["session"]["day"], "2026-09-26")
        self.assertEqual(feed.payload()["session"]["prints"], 1)

    def test_rebuild_deduplicates_overlap_and_marks_restart_gap(self):
        import gzip, io
        events = []
        source = recent.RecentPrints()
        source.observe = lambda e, _o=source.observe: (events.append(e), _o(e))
        for n in range(1, 11):
            self.trade(source, n, 1 + n / 100)
        blob = gzip.compress(bytes([10]).join(json.dumps(e).encode() for e in events))
        client = Mock()
        client.list_objects_v2.return_value = {"Contents": [{"Key": "p/a-part-0000.ndjson.gz"}, {"Key": "p/x.json"}]}
        client.get_object.side_effect = lambda **k: {"Body": io.BytesIO(blob)}
        feed = recent.RecentPrints()
        feed.rebuild(client, "bucket", "p", now=datetime.fromtimestamp((self.T0 + 60000) / 1000, timezone.utc))
        session = feed.payload()["session"]
        self.assertEqual((session["prints"], session["rebuilt"]["status"], session["rebuilt"]["segments"]), (10, "rebuilt", 1))
        self.assertEqual(session["gaps"][-1], dict(start=self.T0 + 15, end=None, reason="collector_restart:rebuilt"))
        for n in range(8, 13):  # 8-10 overlap the archive
            self.trade(feed, n, 1.2, t=self.T0 + 30000 + n)
        session = feed.payload()["session"]
        self.assertEqual(session["prints"], 12)
        self.assertEqual(session["gaps"][-1]["end"], self.T0 + 30013)
        self.assertEqual(session, recent.RecentPrints.payload(feed)["session"])

    def test_failed_rebuild_is_visible_and_not_fatal(self):
        client = Mock()
        client.list_objects_v2.side_effect = OSError()
        feed = recent.RecentPrints()
        feed.rebuild(client, "bucket", "p", now=datetime.fromtimestamp(self.T0 / 1000, timezone.utc))
        session = feed.payload()["session"]
        self.assertEqual(session["rebuilt"]["status"], "rebuild_failed")
        self.assertEqual(session["gaps"], [dict(start=self.T0, end=None, reason="collector_restart:rebuild_failed")])
        self.trade(feed, 1, 1.0)
        self.assertEqual(feed.payload()["session"]["gaps"][0]["end"], self.T0 + 6)

    def test_late_start_with_empty_archive_flags_the_missing_opening(self):
        client = Mock()
        client.list_objects_v2.return_value = {"Contents": []}
        feed = recent.RecentPrints()
        late = self.T0 + 47 * 60000  # collector first starts at 10:17 ET with nothing archived
        feed.rebuild(client, "bucket", "p", now=datetime.fromtimestamp(late / 1000, timezone.utc))
        session = feed.payload()["session"]
        self.assertEqual(session["rebuilt"]["segments"], 0)
        self.assertEqual(session["gaps"], [dict(start=self.T0, end=None, reason="collector_restart:rebuilt")])
        self.trade(feed, 1, 1.0, t=late + 1000)
        self.assertEqual(feed.payload()["session"]["gaps"], [dict(start=self.T0, end=late + 1005, reason="collector_restart:rebuilt")])

    def test_preopen_start_with_empty_archive_has_no_gap(self):
        client = Mock()
        client.list_objects_v2.return_value = {"Contents": []}
        feed = recent.RecentPrints()
        feed.rebuild(client, "bucket", "p", now=datetime.fromtimestamp((self.T0 - 60000) / 1000, timezone.utc))
        self.assertEqual(feed.payload()["session"]["gaps"], [])


class LabelTests(unittest.TestCase):
    """Tiered ``label``/``basis`` signing; ``side`` must keep its original value."""
    SYMBOL = "QQQ260925C00745000"
    T0 = 1790343000000

    @staticmethod
    def stamp(t):
        return datetime.fromtimestamp(t / 1000, timezone.utc).isoformat()

    def quote(self, feed, bid, ask, dated, received):
        feed.observe(dict(type="quote", symbol=self.SYMBOL, bid=bid, ask=ask, biddate=dated, askdate=dated,
                          collector_receipt_timestamp=self.stamp(received)))

    def trade(self, feed, seq, price, t, received=None):
        feed.observe(dict(type="timesale", symbol=self.SYMBOL, seq=seq, date=t, last=price, size=3,
                          collector_receipt_timestamp=self.stamp(received or t + 50)))
        return feed.rows[-1]

    def test_prevailing_quote_is_used_when_the_newest_quote_followed_the_print(self):
        feed = recent.RecentPrints()
        t = self.T0 + 10000
        self.quote(feed, 1.0, 1.2, t - 400, t - 300)
        self.quote(feed, 1.3, 1.5, t + 20, t + 30)  # arrived after the trade time: not prevailing
        row = self.trade(feed, 1, 1.18, t, received=t + 40)
        self.assertEqual(row["side"], "unknown")  # original rule rejects the newest quote
        self.assertEqual((row["label"], row["basis"]), ("buy", "q1"))

    def test_quote_freshness_requires_both_sides(self):
        t = self.T0 + 60000
        for age, basis in ((3000, "q5"), (20000, "q30"), (60000, "none"), (-1, "none")):
            for bid_age, ask_age in ((age, 10), (10, age)):
                with self.subTest(bid_age=bid_age, ask_age=ask_age):
                    feed = recent.RecentPrints()
                    feed.observe(dict(type="quote", symbol=self.SYMBOL, bid=1.0, ask=1.2,
                                      biddate=t - bid_age, askdate=t - ask_age,
                                      collector_receipt_timestamp=self.stamp(t - 5)))
                    row = self.trade(feed, 1, 1.18, t)
                    self.assertEqual(row["basis"], basis)
                    self.assertEqual(row["label"], "unknown" if basis == "none" else "buy")

    def test_regressing_time_does_not_sign_or_replace_the_tick_reference(self):
        feed = recent.RecentPrints()
        t = self.T0 + 5000
        self.trade(feed, 1, 1.00, t)
        self.trade(feed, 2, 1.10, t + 50)
        late = self.trade(feed, 3, 1.30, t, received=t + 150)
        following = self.trade(feed, 4, 1.10, t + 50, received=t + 151)
        self.assertEqual((late["label"], late["basis"]), ("unknown", "none"))
        self.assertEqual((following["label"], following["basis"]), ("buy", "zero_tick"))

    def test_quote_age_tiers(self):
        for age, basis in ((800, "q1"), (3000, "q5"), (20000, "q30")):
            feed = recent.RecentPrints()
            t = self.T0 + 60000
            self.quote(feed, 1.0, 1.2, t - age, t - age + 5)
            row = self.trade(feed, 1, 1.02, t)
            self.assertEqual((row["label"], row["basis"]), ("sell", basis))
        feed = recent.RecentPrints()
        self.quote(feed, 1.0, 1.2, self.T0, self.T0 + 5)
        row = self.trade(feed, 1, 1.02, self.T0 + 31000)
        self.assertEqual((row["label"], row["basis"]), ("unknown", "none"))

    def test_same_millisecond_sweep_is_tick_signed_in_sequence_order(self):
        feed = recent.RecentPrints()
        t = self.T0 + 5000
        rows = [self.trade(feed, 1, 1.00, t), self.trade(feed, 2, 1.05, t), self.trade(feed, 3, 1.05, t)]
        self.assertEqual([r["side"] for r in rows], ["unknown", "unknown", "unknown"])
        self.assertEqual([(r["label"], r["basis"]) for r in rows],
                         [("unknown", "none"), ("buy", "tick"), ("buy", "zero_tick")])

    def test_midpoint_and_locked_stay_unsigned_without_a_tick(self):
        feed = recent.RecentPrints()
        t = self.T0 + 5000
        self.quote(feed, 1.0, 1.2, t - 100, t - 90)
        self.assertEqual((self.trade(feed, 1, 1.1, t)["label"], feed.rows[-1]["basis"]), ("unknown", "mid"))
        feed = recent.RecentPrints()
        self.quote(feed, 1.2, 1.2, t - 100, t - 90)
        self.assertEqual(self.trade(feed, 1, 1.2, t)["basis"], "locked")

    def test_session_label_totals_partition_volume_and_keep_bsu(self):
        feed = recent.RecentPrints()
        t = self.T0 + 5000
        self.trade(feed, 1, 1.00, t)                       # none -> U
        self.trade(feed, 2, 1.05, t)                       # tick buy -> low
        self.quote(feed, 1.0, 1.2, t + 10, t + 20)
        self.trade(feed, 3, 1.19, t + 500)                 # q1 buy (also original-rule buy)
        self.trade(feed, 4, 1.01, t + 3000)                # q5 sell (original rule: tick sell)
        c = feed.payload()["session"]["contracts"][self.SYMBOL]
        self.assertEqual((c["B"], c["S"], c["U"], c["prints"]), (3, 3, 6, 4))
        self.assertEqual(c["label"], dict(q1=[3, 0], q5=[0, 3], low=[3, 0], U=3))
        lab = c["label"]
        self.assertEqual(sum(lab["q1"] + lab["q5"] + lab["low"]) + lab["U"], c["B"] + c["S"] + c["U"])


if __name__ == "__main__":
    unittest.main()
