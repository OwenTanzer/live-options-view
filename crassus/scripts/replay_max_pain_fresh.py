"""Regression comparison on previously inspected boards, never fresh validation.

Run from any directory with --baseline-ref pointing to the pre-patch commit.
The fixture preserves the frozen research parser's selected boards, raw-file
hashes and rows. It cannot reconstruct skipped observations or live fill lag.
"""
import argparse
import gzip
import hashlib
import json
import subprocess
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))
from crassus.client import Book
from crassus.market import MarketSnapshot, Quote
from crassus.strategy import StrategyContext
from crassus.strategies import max_pain_episodes as patched

def baseline(ref):
    source = subprocess.check_output(
        ["git", "show", ref + ":crassus/crassus/strategies/max_pain_episodes.py"],
        cwd=ROOT, text=True)
    # Use the real pre-patch decision function without replacing the registry.
    module = types.ModuleType("crassus.strategies._replay_baseline")
    module.__package__ = "crassus.strategies"
    exec(source.split("for _strategy_id in THRESHOLDS:")[0], module.__dict__)
    return module

def run(module, day, strategy, slip):
    fills, trades, opened = [], [], None
    with TemporaryDirectory() as folder:
        for index, board in enumerate(day["boards"]):
            now = datetime.strptime(day["day"], "%Y%m%d").replace(
                tzinfo=ZoneInfo("America/New_York")) + timedelta(seconds=board["t"])
            stamp = now.isoformat()
            quotes = {r["OptionSymbol"]: r for r in board["rows"]
                      if r["Bid"] is not None and r["Ask"] is not None
                      and 0 < r["Bid"] <= r["Ask"]}
            def quote(symbols):
                return {s: Quote(s, max(0, quotes[s]["Bid"]-slip),
                                quotes[s]["Ask"]+slip, stamp, stamp,
                                bid_ts=stamp, ask_ts=stamp)
                        for s in symbols if s in quotes}
            book = Book(fills)
            request = str(index)
            # Match the runner's mandatory last-15-minutes flatten gate.
            if board["t"] >= day["end"]-900:
                if book.is_flat:
                    continue
                symbol = next(iter(book.positions))
                if symbol not in quotes:
                    continue
                action, reason = "sell", "flatten"
            else:
                snapshot = MarketSnapshot.from_payload("fixture://max-pain-67", {
                    "timestamp": stamp, "expiration": now.date().isoformat(),
                    "underlying_price": board["spot"], "rows": board["rows"]}, b"{}")
                ctx = StrategyContext(snapshot, {"username": strategy}, book,
                    now, "open", quote, state_dir=Path(folder), decision_id=request)
                decision = module._decide(ctx, strategy)
                if not decision.is_trade:
                    continue
                symbol, action, reason = decision.symbol, decision.action, decision.reason
            price = quotes[symbol]["Ask"]+slip if action == "buy" else max(0,quotes[symbol]["Bid"]-slip)
            fills.append(dict(sym=symbol, side=action, qty=1, price=price,
                              ts=stamp, execution_request_id=request))
            if action == "buy":
                opened = dict(entry=stamp, symbol=symbol, paid=price)
            else:
                trades.append(dict(**opened, exit=stamp, received=price,
                                   reason=reason, net=round((price-opened["paid"])*100-1.30,2)))
                opened = None
        if opened:
            trades.append(dict(**opened, exit=None, received=0, reason="unresolved",
                               net=round(-opened["paid"]*100-1.30,2)))
    return trades

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="1c48553")
    parser.add_argument("--boards", type=Path, default=Path(__file__).parent/"fixtures/max-pain-67-boards.json.gz")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    raw=args.boards.read_bytes()
    days=json.loads(gzip.decompress(raw))
    old=baseline(args.baseline_ref)
    results={}
    for strategy in patched.THRESHOLDS:
        daily=[]
        for day in days:
            arms={label:run(mod,day,strategy,slip) for label,mod,slip in (
                ("current",old,0),("patched",patched,0),
                ("current_adverse_cent",old,.01),("patched_adverse_cent",patched,.01))}
            oldkeys={(t["entry"],t["symbol"]) for t in arms["current"][1:]}
            newkeys={(t["entry"],t["symbol"]) for t in arms["patched"][1:]}
            daily.append(dict(day=day["day"],trades=arms,
                net={k:round(sum(t["net"] for t in v),2) for k,v in arms.items()},
                removed_reentries=len(oldkeys-newkeys), added_reentries=len(newkeys-oldkeys)))
        totals={k:round(sum(d["net"][k] for d in daily),2) for k in daily[0]["net"]}
        results[strategy]=dict(totals=totals, daily=daily,
            trade_counts={k:sum(len(d["trades"][k]) for d in daily) for k in totals},
            removed_reentries=sum(d["removed_reentries"] for d in daily),
            added_reentries=sum(d["added_reentries"] for d in daily),
            paired_net_change=round(totals["patched"]-totals["current"],2))
        print(strategy, {k:v for k,v in results[strategy].items() if k!="daily"}, flush=True)
    report=dict(kind="regression_on_previously_inspected_sessions", baseline=args.baseline_ref,
        candidate=candidate,
        sessions=len(days),boards=sum(len(d["boards"]) for d in days),
        input_sha256=hashlib.sha256(raw).hexdigest(),results=results)
    args.output.write_text(json.dumps(report,indent=2)+"\n")

if __name__ == "__main__":
    main()
