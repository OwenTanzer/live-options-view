"""Offline adapter checks, including normal Crassus/Worker share fills and restart."""
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))
from crassus import clock
from crassus.client import Book
from crassus.config import Account
from crassus.market import Quote
from crassus.runner import Runner
from crassus.strategies import oi_associator as oi
from crassus.strategy import StrategyContext, get
from harness_process import start_harness

START = datetime(2026,11,23,9,35,tzinfo=clock.ET)
DAY = START.date()
ASOF = int(START.timestamp()*1000)
LEVELS = [(9652,6739),(9748,3769),(4907,6023),(1342,3288),(10397,9344),(8903,627),(1426,6756)]
PACKET = {"session": str(DAY), "etag": "synthetic", "availableAt": ASOF-2000, "capturedAt": ASOF-2100,
          "levels":[{"strike":741+i,"callOI":a,"putOI":b} for i,(a,b) in enumerate(LEVELS)],
          "calibration":{"openingSpot":744.02,"openingAt":ASOF-300000,"settledSession":"2026-11-20",
                         "openingEvidence":"intraday/20261123/opening-oi/v1/0001.json","tier":"0DTE_Regular"}}

def context(at=START, trades=None):
    trades=trades or []
    stamp=at.isoformat()
    quote=Quote("QQQ",743.99,744.01,stamp,stamp,"equity",stamp,stamp)
    return StrategyContext(None,{"trade_count":len(trades),"fetched_at":stamp,"balance_cash":10000},
                           Book(trades),at,"open",lambda _: {"QQQ":quote})

def fill(side, when):
    return dict(side=side,ts=when.isoformat(),sym="QQQ",qty=1,price=744.01,instrument_type="share")


class Adapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packet=copy.deepcopy(PACKET)
        cls.requests=[]
        class Feed(BaseHTTPRequestHandler):
            def log_message(self,*a): pass
            def do_GET(self):
                target=urlsplit(self.path)
                cls.requests.append((target.path,parse_qs(target.query)))
                body=json.dumps(cls.packet).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
        cls.feed=ThreadingHTTPServer(("127.0.0.1",0),Feed)
        cls.thread=threading.Thread(target=cls.feed.serve_forever,daemon=True); cls.thread.start()
        cls.base=patch.object(oi,"BASE_URL","http://127.0.0.1:"+str(cls.feed.server_port));cls.base.start()

    @classmethod
    def tearDownClass(cls):
        cls.base.stop();cls.feed.shutdown();cls.feed.server_close();cls.thread.join()

    def setUp(self):
        type(self).packet=copy.deepcopy(PACKET)
        oi.frozen_path.cache_clear();oi.opening.cache_clear();type(self).requests.clear()

    def test_four_registered_real_forecasts_share_frozen_cutoff(self):
        for name in oi.IDS:
            d=get(name)(context(START+timedelta(minutes=3)))
            self.assertEqual((d.action,d.symbol,d.quantity,d.instrument_type,d.limit_price),("buy","QQQ",1,"share",744.01))
            self.assertGreater(d.metadata["forecast_bps"][-1],2)
            self.assertEqual(d.metadata["trained_through"],"2026-07-31")
        self.assertEqual(self.requests,[("/api/one-clock/opening-oi",{"session":[str(DAY)],"cutoff":[str(ASOF)]})])
        rows=json.loads((ROOT/"crassus"/"accounts.example.json").read_text())["accounts"]
        bots=[r for r in rows if r["strategy_id"] in oi.IDS]
        self.assertEqual(len(bots),4)
        self.assertEqual(len({r["username"] for r in bots}),4)
        self.assertTrue(all(r["active"] is False and "password" not in r for r in bots))

    def test_clock_history_quotes_and_cash(self):
        bot=get("oi_associator_ridge")
        for delta in (-1,300):
            self.assertEqual(bot(context(START+timedelta(seconds=delta))).action,"no_trade")
        ctx=context();ctx.account_state["balance_cash"]=700
        self.assertEqual(bot(ctx).action,"no_trade")
        h=[fill("buy",START),fill("sell",START+timedelta(seconds=5))]
        self.assertEqual(bot(context(START+timedelta(seconds=7),h)).action,"no_trade")
        ctx=context();ctx.account_state["trade_count"]=1
        self.assertEqual(bot(ctx).action,"no_trade")
        ctx=context();ctx.quotes=lambda _:{"QQQ":Quote("QQQ",744,745,START.isoformat(),START.isoformat(),
                  "equity",START.isoformat(),(START-timedelta(seconds=17)).isoformat())}
        self.assertEqual(bot(ctx).action,"no_trade")
        ctx=context();ctx.quotes=lambda _:{"QQQ":Quote("QQQ",744,745,START.isoformat(),START.isoformat(),
                  "option",START.isoformat(),START.isoformat())}
        self.assertEqual(bot(ctx).action,"no_trade")
        with patch.object(oi,"frozen_path",return_value={"forecast_bps":[100,100,2]}):
            self.assertEqual(bot(context()).action,"no_trade")

    def test_missing_or_late_input_and_independent_exit(self):
        for change in ("late","oi","session","settled"):
            with self.subTest(change=change):
                type(self).packet=copy.deepcopy(PACKET)
                if change=="late": self.packet["availableAt"]=ASOF+1
                elif change=="oi": del self.packet["levels"][0]["callOI"]
                elif change=="session": self.packet["session"]="2026-11-20"
                else: self.packet["calibration"]["settledSession"]="2026-11-19"
                oi.opening.cache_clear();oi.frozen_path.cache_clear()
                self.assertEqual(get("oi_associator_forest")(context()).action,"no_trade")
        type(self).packet={}
        self.assertEqual(get("oi_associator_forest")(context(START.replace(hour=15,minute=30),[fill("buy",START)])).action,"sell")

    def test_runner_and_real_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            file=root/"clock"
            now=START+timedelta(minutes=1)
            file.write_text(str(int(now.timestamp()*1000)))
            with patch.dict(os.environ,{"CRASSUS_TEST_CLOCK_FILE":str(file)}):
                proc, _, base=start_harness(shutil.which("node"),ROOT/"crassus/scripts/sma200_worker_harness.mjs")
            try:
                accounts=[Account(name,name,"synthetic-password",name) for name in oi.IDS]
                def runner():
                    r=Runner(accounts,base_url=base,snapshot_url=base+"/unused",ledger_dir=root/"ledger",
                             state_dir=root/"state",bot_registration_key="sma-integration-key")
                    # All traffic stays on loopback. Remove the production wait between accounts.
                    r.rate_limiter.acquire=lambda:None
                    r.startup()
                    return r
                with patch.object(clock,"now_et",lambda:now), patch.object(clock,"now_utc",lambda:now):
                    r=runner()
                    for account in accounts:
                        r._run_account(account,None,"open")
                        trades=r.sessions[account.alias].me().trades
                        self.assertEqual([(t["side"],t["instrument_type"],t["qty"]) for t in trades],[("buy","share",1)])
                    r=runner()
                    for account in accounts:
                        r._run_account(account,None,"open")
                        self.assertEqual(len(r.sessions[account.alias].me().trades),1)
                    oi.opening.cache_clear();oi.frozen_path.cache_clear();type(self).packet={}
                    for j,account in enumerate(accounts):
                        # The last account misses the scheduled exit and recovers through mandatory flatten.
                        now=START.replace(hour=15,minute=30 if j<3 else 50)
                        file.write_text(str(int(now.timestamp()*1000)))
                        r._run_account(account,None,"open")
                        state=r.sessions[account.alias].me()
                        self.assertEqual([(t["side"],t["sym"],t["qty"],t["instrument_type"]) for t in state.trades],
                                         [("buy","QQQ",1,"share"),("sell","QQQ",1,"share")])
                        self.assertAlmostEqual(state.trades[-1]["price"],499.9)
            finally:
                proc.terminate();proc.wait(timeout=5)
                if proc.stdout: proc.stdout.close()

if __name__=="__main__":
    unittest.main()
