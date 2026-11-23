"""Offline proof of QQQ session identity, null OI and create-only restart behavior."""
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from market_data.live_market.opening_oi import publish_opening_oi
from market_data.live_market.feed import DXLinkFeed


class OpeningOiTest(unittest.TestCase):
    def test_qualified_summary_create_only_and_opening_window(self):
        start = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
        day_id = lambda d: (d.date()-datetime(1970, 1, 1).date()).days
        feed = DXLinkFeed("local", "fixture")
        feed._ingest([{"eventType":"Summary","eventSymbol":"QQQ","dayOpenPrice":700.5,"dayId":day_id(start)},
                      {"eventType":"Summary","eventSymbol":"call","openInterest":0,"prevDayId":day_id(start)-3},
                      {"eventType":"Summary","eventSymbol":"put","openInterest":999,"prevDayId":day_id(start)-4}])
        writes, reads = {}, []
        class Existing(Exception):
            response={"Error":{"Code":"PreconditionFailed"}}
        class Bucket:
            def list_objects_v2(self, **kw):
                return {"Contents":[{"Key":k} for k in sorted(writes) if k.startswith(kw["Prefix"])]}
            def get_object(self, **kw):
                reads.append(kw["Key"])
                if kw["Key"] in writes:
                    return {"Body":io.BytesIO(writes[kw["Key"]])}
                return {"ETag":"range1","LastModified":start-timedelta(days=5),"Body":io.BytesIO(b"opening calibration")}
            def put_object(self, **kw):
                assert kw["IfNoneMatch"]=="*"
                if kw["Key"] in writes: raise Existing()
                writes[kw["Key"]]=kw["Body"]
        kwargs=dict(payload={"symbol":"QQQ","date":"2026-11-23","expiration":"2026-11-23","tier":"0DTE_Regular","snapshot_key":"opening-csv"},
                    state=feed.get_state(),strikes=[{"strike":700,"call_sym":"call","put_sym":"put"}],session_open=start,previous_session="2026-11-20",cursor={})
        def capture(seconds):
            publish_opening_oi(Bucket(),"fixture",captured=start+timedelta(seconds=seconds),**kwargs)
        capture(-1)
        self.assertEqual(reads,[])
        capture(5)
        key="intraday/20261123/opening-oi/v1/0001.json"
        initial=writes[key]
        self.assertEqual(json.loads(initial)["legs"],[{"strike":700,"call":0,"put":None}])
        # The restarted writer sees a later official open and changed call
        # count. Only the previously unavailable put may fill in.
        kwargs["state"]["QQQ"]["day_open"]=900
        kwargs["state"]["call"]["oi"]=1000
        kwargs["state"]["put"]["oi_for_date"]="2026-11-20"
        kwargs["cursor"]={}
        capture(3600)
        second=json.loads(writes["intraday/20261123/opening-oi/v1/0002.json"])
        self.assertEqual(second["legs"],[{"strike":700,"call":0,"put":999}])
        self.assertEqual(second["openingSpot"],700.5)
        self.assertEqual(writes[key],initial)
        capture(7200)
        self.assertEqual(len(writes),2)
        self.assertEqual(reads.count("derived/OIranges.csv"),1)
