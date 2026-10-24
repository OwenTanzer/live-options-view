import requests,json,hashlib,io,re
from pathlib import Path
import pandas as pd,numpy as np
R=Path(__file__).resolve().parents[2];O=R/'outputs'/'round2';D=R/'work'/'private'/'round2-sources';D.mkdir(exist_ok=True);B='https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/'
source=json.loads((R/'work'/'round2'/'source-manifest.txt').read_text());objects=source['objects'];dates=sorted(d for d in objects if d>'20260805')[:3];keys=['manifest.json'];expected={}
for day in dates:
    ordered=sorted(objects[day],key=lambda x:x['key'])
    for target in ['093000','093500']:
        o=next(x for x in ordered if re.search(r'snapshot_(\d{6})',x['key']).group(1)>=target);keys.append(o['key']);expected[o['key']]=o.get('sha256')
(O/'options-discovery-plan.json').write_text(json.dumps({'frozen_utc':pd.Timestamp.now(tz='UTC').isoformat(),'keys_first_pass':keys,'selection':'first three indexed sessions strictly after frozen final end; first snapshot at/after09:30 and09:35. Schema/timing audit only, not forecasts.','R2_object_budget':12,'index_provenance':{'generated_at_utc':source['generated_at_utc'],'base_revision':source['git_base_revision'],'index_sha256':hashlib.sha256((R/'work'/'round2'/'source-manifest.txt').read_bytes()).hexdigest()},'indexed_sessions':{k:len(v) for k,v in objects.items()}},indent=2));out=[]
for k in keys:
    r=requests.get(B+k,timeout=20);rec={'key':k,'status':r.status_code,'retrieved_utc':pd.Timestamp.now(tz='UTC').isoformat()}
    if r.status_code!=200:out.append(rec);continue
    raw=r.content;(D/k.replace('/','__')).write_bytes(raw);rec.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),last_modified=r.headers.get('Last-Modified'));rec['matches_historical_hash']=rec['sha256']==expected[k] if k in expected else None
    if k.endswith('.json'):
        d=r.json();rec['fields']=list(d);rec['weekly_expirations_count']=len(d.get('weekly_expirations',{}));rec['weekly_keys_sample']=list(d.get('weekly_expirations',{}).items())[-2:]
    else:
        f=pd.read_csv(io.BytesIO(raw));rec.update(rows=len(f),fields=list(f),missing=f.isna().sum().to_dict(),duplicate_contracts=int(f.OptionSymbol.duplicated().sum()),zero_bid=int(f.Bid.eq(0).sum()),crossed=int(f.Bid.gt(f.Ask).sum()),expired_contract_rows=int((pd.to_datetime(f.Expiration).dt.date<pd.to_datetime(f.TradeDate).dt.date).sum()),negative_volume_delta=int(f.VolDelta.lt(0).sum()),median_relative_spread=float(((f.Ask-f.Bid)/((f.Ask+f.Bid)/2)).median()),timestamp_columns=[c for c in f if re.search('time|_ts|date',c,re.I)])
    out.append(rec)
(O/'options-discovery.json').write_text(json.dumps(out,indent=2,default=str));print(json.dumps(out,indent=2,default=str)[:15000]);print('OPTIONS_DISCOVERY_COMPLETE',len(keys))
