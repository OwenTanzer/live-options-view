import json,requests,hashlib,io
from pathlib import Path
import pandas as pd
R=Path(__file__).resolve().parents[1];D=R/'work'/'private';O=R/'outputs';B='https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/'
keys=['history/QQQ/options_0dte/2025-04-01.json','intraday/20261023/snapshot_161411543301.csv','intraday/spy/20261023/snapshot_161413479278.csv'];out=[]
for k in keys:
    r=requests.get(B+k,timeout=20);r.raise_for_status();b=r.content;(D/k.replace('/','__')).write_bytes(b)
    d=json.loads(b) if k.endswith('.json') else None
    f=pd.DataFrame(d) if d is not None and all(isinstance(x,list) for x in d.values()) else pd.read_csv(io.BytesIO(b)) if d is None else None
    rec={'key':k,'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b),'last_modified':r.headers.get('Last-Modified'),'fields':list(d) if d is not None else list(f),'metadata':{a:v for a,v in d.items() if not isinstance(v,list)} if d else None}
    if f is not None:
        rec['rows']=len(f);rec['missing']=f.isna().sum().to_dict()
        for c in ['Expiration','DTE','Type','TradeDate','expiration','updated','quoteTime','underlyingPrice']:
            if c in f:rec[c+'_values']=f[c].unique().tolist()[:20]
        if 'Bid' in f:
            rec['crossed']=int((f.Bid>f.Ask).sum());rec['zero_bid']=int((f.Bid==0).sum());rec['median_relative_spread']=float(((f.Ask-f.Bid)/((f.Ask+f.Bid)/2)).median());rec['duplicate_contracts']=int(f.OptionSymbol.duplicated().sum())
    out.append(rec)
(O/'options-feasibility.json').write_text(json.dumps(out,indent=2,default=str));print(json.dumps(out,indent=2,default=str)[:14000])
