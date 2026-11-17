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


if __name__ == "__main__":
    unittest.main()
