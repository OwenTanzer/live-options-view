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
