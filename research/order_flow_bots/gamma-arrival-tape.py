"""Retain public receipt-clock QQQ evidence under an explicitly named study directory."""
import argparse, concurrent.futures, gzip, hashlib, json, pathlib, urllib.error, urllib.request
ORIGIN="https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/"
def read_minute(t,directory):
    key=f"order-flow/qqq/intraminute/v1/{t}.json"
    try:
        with urllib.request.urlopen(urllib.request.Request(ORIGIN+key,headers={"User-Agent":"gamma-arrival-study/1.0"}),timeout=25) as r:
            body=r.read()
    except urllib.error.HTTPError as e:
        if e.code==404: return {"minute":t,"missing":True,"key":key}
        raise
    data=json.loads(body)
    if data["minute"]!=t or data["version"]!=1 or not isinstance(data["rows"],list):
        raise ValueError("unexpected minute evidence")
    with gzip.open(directory/f"{t}.json.gz","wb") as out: out.write(body)
    return {"minute":t,"key":key,"sha256":hashlib.sha256(body).hexdigest(),"source":data["source"],"rows":len(data["rows"])}
if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("start",type=int);p.add_argument("end",type=int);p.add_argument("directory",type=pathlib.Path)
    args=p.parse_args()
    if args.start%60000 or args.end%60000 or not 0<args.end-args.start<=390*60000: p.error("expected complete UTC minutes in one market session; end exclusive")
    args.directory.mkdir()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        manifest=list(pool.map(lambda t:read_minute(t,args.directory),range(args.start,args.end,60000)))
    (args.directory/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
