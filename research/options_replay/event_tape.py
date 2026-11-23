"""Build five-second QQQ practice boards from finalized pre-freeze Tradier captures."""
import gzip
import hashlib
import json
import math
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
from pathlib import Path
import archive as A


def build(arc, day):
    root = arc.cache_dir / "event_tape"
    root.mkdir(exist_ok=True)
    prefix = f"moo144/tradier/{day}/"
    summaries = [json.loads(arc.get(k)) for k in arc.list_keys(prefix) if "/summary-" in k]
    report, = [r for r in summaries if r["status"] == "complete"]
    opening = datetime.fromisoformat(report["session_open"])
    midnight = opening.replace(hour=0, minute=0).timestamp()
    start = int(opening.timestamp()-midnight)
    end = int(datetime.fromisoformat(report["session_close"]).timestamp()-midnight)
    symbol_re = re.compile("QQQ"+day.replace("-","")[2:]+r"([CP])(\d{8})$")
    events = defaultdict(dict)
    prints = defaultdict(list)
    volumes = defaultdict(int)
    parts = report["event_parts"]
    def read(part):
        body = arc.get(part["key"])
        if hashlib.sha256(body).hexdigest() != part["sha256"]:
            raise ValueError("archive part digest changed")
        return body
    # Consume parts in receipt order. Index the original row for independent fill reconciliation.
    with ThreadPoolExecutor(6) as pool:
        for part, body in zip(parts, pool.map(read,parts)):
            for line, raw in enumerate(gzip.decompress(body).splitlines()):
                r=json.loads(raw)
                receipt=datetime.fromisoformat(r["collector_receipt_timestamp"]).timestamp()-midnight
                sym=r.get("symbol","")
                if r.get("type")=="quote" and (match:=symbol_re.fullmatch(sym)):
                    btime=int(r["biddate"])/1000-midnight
                    atime=int(r["askdate"])/1000-midnight
                    t=5*math.ceil(max(receipt,btime,atime)/5)
                    if not start<=t<=end or min(btime,atime)<t-5: continue
                    contract=match[1]+A._fmt_strike(int(match[2])/1000)
                    entry=[max(receipt,btime,atime),r["bid"],r["ask"],part["key"],line]
                    if contract not in events[t] or events[t][contract][0]<=entry[0]:
                        events[t][contract]=entry
                elif sym=="QQQ" and r.get("type")=="timesale" and r.get("session")=="normal" and not any(r.get(k) for k in ("cancel","correction","duplicate_in_run")):
                    observed=max(receipt,int(r["date"])/1000-midnight)
                    t=5*math.ceil(observed/5)
                    trade_time = int(r["date"])/1000-midnight
                    if start <= trade_time <= end and start <= t <= end and observed-trade_time <= 5:
                        # UTP specification 3.14.2. Live candles use trades eligible to
                        # set both consolidated last and range. A restrictive modifier
                        # still applies when combined with the regular-sale marker.
                        flags = set((r.get("flag") or "").strip())
                        if not flags.intersection("MQ9"):
                            volumes[t] += int(r["size"])
                        if flags and flags <= set("@ABDFKLOSXY156 "):
                            prints[t].append((observed,line,float(r["last"])))
    snaps=[]
    for t in range(start,end+1,5):
        trades=sorted(prints[t]); board=events[t]
        prices = [trade[2] for trade in trades]
        spot=prices[-1] if prices else None
        snaps.append(dict(t=t,key=f"tape/{day}/{t}",spot=spot,ohlc=[prices[0],max(prices),min(prices),prices[-1]] if prices else None,volume=volumes[t],
                          q={c:e[1:3] for c,e in board.items()},refs={c:e[3:] for c,e in board.items()},
                          ok=bool(spot and board),reason=None if spot and board else "gap",content_t=t))
    pack=dict(day=day,snapshots=snaps,event_tape=True,open_s=start,close_s=end,volume_unit="QQQ shares",
              coverage_usable=A.playable_coverage(snaps,start,end))
    dest=root/(day+".json.gz")
    tmp=dest.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(json.dumps(pack,separators=(",",":")).encode(),compresslevel=1))
    tmp.replace(dest)
    print(day,len(snaps),sum(s['ok'] for s in snaps),flush=True)


@lru_cache(maxsize=4)
def load(path):
    return json.loads(gzip.decompress(Path(path).read_bytes()))


def raw_lookup(arc, pack):
    by_key={s["key"]:s for s in pack["snapshots"]}
    def lookup(key,contract):
        snap=by_key[key]
        part,line=snap["refs"][contract]
        row=json.loads(gzip.decompress(arc.get(part)).splitlines()[line])
        return dict(key=part,OptionSymbol=row["symbol"],Bid=row["bid"],Ask=row["ask"],UnderlyingPrice=snap["spot"])
    return lookup


if __name__=="__main__":
    arc=A.Archive()
    for d in ("2026-11-13","2026-11-12","2026-11-11"):
        build(arc,d)
