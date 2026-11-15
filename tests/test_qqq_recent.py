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

if __name__ == "__main__":
    unittest.main()
