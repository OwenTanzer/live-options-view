import gzip
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
from threading import Event
from unittest.mock import Mock
import unittest

from market_data.tradier.intraminute import safe_project
from market_data.tradier.intraminute_live import Tape
from market_data.tradier.intraminute_minutes import publish_closed
from market_data.tradier.recent import RecentPrints


def quote(at, symbol="QQQ"):
    return dict(type="quote",symbol=symbol,collector_receipt_timestamp=datetime.fromtimestamp(
        at/1000,timezone.utc).isoformat(),bid=700,ask=701,biddate=at-400,askdate=at-380)


def compress(events):
    return gzip.compress("".join(json.dumps(e)+"\n" for e in events).encode())


class Projection(unittest.TestCase):
    def test_quote_provenance_and_resumption(self):
        tape=Tape(RecentPrints(),limit=2)
        for t in (10,20,30):
            tape.observe(quote(t))
        self.assertIsNone(safe_project(quote(20,"SPY")))
        self.assertIsNone(safe_project(dict(type="quote",symbol="QQQ")))
        batch=tape.read(0,"old")
        self.assertEqual([r["at"] for r in batch["rows"]],[20,30])
        self.assertEqual(batch["rows"][0]["askAt"],-360)
        self.assertTrue(batch["truncated"])
        self.assertEqual(tape.read(batch["cursor"],batch["epoch"])["rows"],[])
        self.assertTrue(tape.read(0,batch["epoch"])["reset"])

    def test_closed_minute_uses_only_derived_key(self):
        tape=Tape(RecentPrints())
        tape.started=0
        for at in (60010,60020,120010):
            tape.observe(quote(at))
        client=Mock()
        stop=Event()
        client.put_object.side_effect=lambda **kw: stop.set()
        publish_closed(tape,client,"local",stop,Event())
        args=client.put_object.call_args.kwargs
        self.assertEqual(args["Key"],"order-flow/qqq/intraminute/v1/60000.json")
        self.assertEqual(args["IfNoneMatch"],"*")

    def test_option_signing_and_stock_never_cross(self):
        tape=Tape(RecentPrints())
        base=1794580560000
        symbol="QQQ261113C00685000"
        tape.observe(quote(base,symbol))
        for sym in [symbol,symbol,"QQQ"]:
            tape.observe(dict(quote(base+100,sym),type="timesale",seq=10,date=base+80,last=701,size=3,flag="@"))
        rows=tape.read(0,"")["rows"]
        self.assertEqual([r["kind"] for r in rows],["option","stock"])
        self.assertEqual((rows[0]["basis"],rows[0]["label"]),("q1","buy"))

    def test_same_sequence_amendments_through_stats_live_and_archive(self):
        from market_data.tradier.stats import BoundedStats
        from market_data.tradier.intraminute import collect
        stats = BoundedStats()
        tape = Tape(RecentPrints())
        events = []
        for offset, change in enumerate(({}, {"cancel": True}, {"correction": True},
                                         {"correction": True, "last": 702})):
            event = dict(quote(60010 + offset), type="timesale", seq=123, date=60005,
                         last=701, size=2, flag="@")
            event.update(change)
            events.append(stats.observe(event))
            tape.observe(events[-1])
        self.assertTrue(all(e["duplicate_in_run"] for e in events[1:]))
        live = tape.read(0, "")["rows"]
        self.assertEqual(len(live), 4)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "part.gz"
            path.write_bytes(compress(events))
            self.assertEqual(collect([path], 60000, 120000)[60000], live)

    def test_multiminute_outage_survives_provisional_and_final_replay(self):
        from market_data.tradier.intraminute_archive import publish, replayable
        def gap(at, reason, duration=0):
            return dict(type="gap", receipt_timestamp=quote(at)["collector_receipt_timestamp"],
                        reason=reason, outage_seconds=duration)
        events = [quote(60120), gap(119900, "stream_disconnect"),
                  gap(180100, "stream_reconnect_resumed", 60.2),
                  quote(180120), quote(230000), quote(240020)]
        tape = Tape(RecentPrints())
        tape.started = 60000
        for event in events:
            tape.observe(event)
        provisional = {}
        client = Mock()
        stop = Event()
        def save(**kw):
            body = json.loads(kw["Body"])
            provisional[body["minute"]] = body["rows"]
            if body["minute"] == 180000:
                stop.set()
        client.put_object.side_effect = save
        publish_closed(tape, client, "local", stop, Event())
        self.assertEqual([r["at"] for r in provisional[180000]], [180000,180100,180120,230000])
        self.assertEqual(provisional[120000][0]["reason"], "stream_disconnect")
        prefix = "moo144/tradier/2026-11-13/"
        part = prefix + "abc-part-0000.ndjson.gz"
        compressed = compress(events)
        summary = dict(underlying="QQQ", status="partial", partial_reasons=["reconnects=1"],
                       session_open=quote(60000)["collector_receipt_timestamp"],
                       session_close=quote(300000)["collector_receipt_timestamp"],
                       event_parts=[dict(key=part, sha256=hashlib.sha256(compressed).hexdigest())])
        self.assertFalse(replayable(dict(summary, partial_reasons=["upload_spool_not_fully_drained"])))
        client.get_object.side_effect = lambda Bucket,Key: {"Body": io.BytesIO(
            compressed if Key==part else json.dumps(summary).encode())}
        final = {}
        def save_final(**kw):
            body=json.loads(kw["Body"])
            final[body["minute"]]=body["rows"]
        client.put_object.side_effect=save_final
        self.assertEqual(publish(client,"local",prefix+"summary-abc.json"),4)
        for minute, rows in provisional.items():
            self.assertEqual(final[minute], rows)
