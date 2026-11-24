"""Download immutable QQQ chain evidence and its dated publication receipt for the experiment."""
import argparse
import concurrent.futures
import hashlib
import json
import pathlib
import re
import urllib.request

ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/"
PREFIX = "publications/v1/collector-chain/QQQ/"

def get(key):
    request = urllib.request.Request(ORIGIN+key, headers={"User-Agent":"gamma-arrival-study/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()

def read_sequence(seq, day):
    key = PREFIX + f"entries/{seq:012d}.json"
    raw = get(key)
    entry = json.loads(raw)
    if entry["stream"] != "collector-chain/QQQ" or entry["sequence"] != seq:
        raise ValueError("unexpected stream/sequence")
    price = next(r for r in entry["records"] if r["source"]=="price")
    if price["session"] != day:
        return None
    snapshot = price["id"]
    if not re.fullmatch(r"intraday/"+day.replace("-","")+r"/snapshot_\d{12}\.csv",snapshot):
        raise ValueError("unexpected snapshot key")
    receipt = json.loads(get(snapshot[:-4]+".provenance.json"))
    pr = receipt.get("one_clock_publication") or {}
    if receipt["snapshot_key"] != snapshot or pr.get("digest") != hashlib.sha256(raw).hexdigest() or pr.get("key") != key:
        raise ValueError("sidecar and immutable publication disagree")
    arrival = receipt.get("latest_published_at")
    if receipt["publication"]["status"] != "published" or not arrival:
        return {"key":snapshot,"sequence":seq,"excluded":"did not become latest"}
    ref = next(e for e in entry["evidence"] if e["schema"]=="collector.snapshot.payload.v1")
    if not re.fullmatch(r"[a-f0-9]{64}",ref["sha256"]) or ref["key"]!=PREFIX+"blobs/"+ref["sha256"]:
        raise ValueError("unexpected payload key")
    raw = get(ref["key"])
    if hashlib.sha256(raw).hexdigest()!=ref["sha256"] or len(raw)!=ref["bytes"]:
        raise ValueError("payload digest or byte count mismatch")
    payload=json.loads(raw)
    if payload["symbol"]!="QQQ" or payload["date"]!=day or payload["expiration"]!=day or payload["snapshot_key"]!=snapshot:
        raise ValueError("payload snapshot mismatch")
    return {"key":snapshot,"sequence":seq,"digest":ref["sha256"],"capture":payload["timestamp"],
            "arrival":arrival,"rows":[{"symbol":r["OptionSymbol"],"gamma":r.get("Gamma")} for r in payload["rows"]]}

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("day");p.add_argument("first",type=int);p.add_argument("last",type=int);p.add_argument("output",type=pathlib.Path)
    args=p.parse_args()
    if not 0 < args.first <= args.last or args.last-args.first>=700:
        p.error("expected bounded inclusive sequence range of up to 700 entries")
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool, args.output.open("x") as out:
        for row in pool.map(lambda n:read_sequence(n,args.day),range(args.first,args.last+1)):
            if row: out.write(json.dumps(row,separators=(",",":"))+"\n")
