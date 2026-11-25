"""Exercise the production QQQ collector against an independently enumerated provider chain."""
import gzip
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import test_moo144_tradier_collector as fixtures
from market_data.tradier import recent, run


class FullChainCoverage(unittest.TestCase):
    def test_provider_to_archive_and_publication(self):
        scenario = fixtures.SharedStreamMainTests()
        scenario.setUp()
        day = "260908"
        options = [
            dict(symbol=f"QQQ{day}{side}{strike * 1000:08d}", strike=strike, option_type=kind)
            for strike in range(450, 677)
            for side, kind in (("P", "put"), ("C", "call"))
        ]
        # Include a listed strike with a single leg and a contract that never trades.
        options.pop()
        expected = {r["symbol"] for r in options}
        active = options[0]["symbol"]
        quiet = options[-1]["symbol"]
        trade = dict(type="timesale", symbol=active, date=scenario.open_ms + 1000,
                     seq=41, last="0.64", size=3, session="normal")
        client = scenario.client(stream_lines=scenario.lines(ibit=False))
        provider_get = client.get
        def rest(path, **params):
            if path == "/markets/options/chains":
                self.assertEqual((params["symbol"], params["expiration"]), ("QQQ", "2026-09-08"))
                return {"options": {"option": options}}
            return provider_get(path, **params)
        client.get = rest
        socket = Mock()
        frames = iter([{"type": "quote", "symbol": "QQQ"}, trade])
        def recv():
            event = next(frames, None)
            if event is None:
                scenario.end_stream()
                return '{"type":"heartbeat"}'
            if event is trade:
                scenario.to_open()
            return json.dumps(event)
        socket.recv.side_effect = recv
        feeds = []
        def projection(*args):
            feed = recent.RecentPrints(*args)
            original = feed.rebuild
            def rebuild(*args, **kwargs):
                return original(*args, now=scenario.clock(), **kwargs)
            feed.rebuild = rebuild
            feeds.append(feed)
            return feed
        with tempfile.TemporaryDirectory() as tmp, patch.object(run, "RecentPrints", side_effect=projection), patch(
            "websocket.create_connection", return_value=socket
        ):
            result, r2 = scenario.run_main(tmp, client, "QQQ", env={"MOO144_STRIKE_COUNT": "8"})
        self.assertEqual(result, 0)
        started = fixtures.only_json(r2, fixtures.QQQ_PREFIX, "run-started-")
        self.assertEqual(set(started["universe"]["option_symbols"]), expected, "startup subscription membership")
        sent = json.loads(socket.send.call_args.args[0])["symbols"]
        self.assertCountEqual(sent, ["QQQ", *expected], "wire subscription membership")
        keys = [k for (b,k) in r2.objects if k.startswith(fixtures.QQQ_PREFIX + "/") and k.endswith(".ndjson.gz")]
        archived = [json.loads(line) for k in keys for line in gzip.decompress(r2.objects["bucket",k]).splitlines()]
        self.assertIn((active, 41), [(r.get("symbol"), r.get("seq")) for r in archived])
        # Use the real writer once after the fast fixture's capture thread has drained.
        stop = Mock()
        stop.wait.side_effect = [False, True]
        feeds[0].publish(r2, "bucket", stop, threading.Event())
        public = json.loads(r2.objects["bucket",recent.KEY])
        self.assertEqual(set(public["session"]["opening"]), expected, "published subscription membership")
        self.assertIsNone(public["session"]["opening"][quiet])
        self.assertIn((active, 41), [(r["symbol"], r["seq"]) for r in public["rows"]])

