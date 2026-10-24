import json,hashlib,gzip,io
from pathlib import Path
import requests,pandas as pd
R=Path(__file__).resolve().parents[2];D=R/'work/private/round2-sources';O=R/'outputs/round2'
m=json.loads((D/'manifest.json').read_text());weekly=m['weekly_expirations'][sorted(m['weekly_expirations'])[0]]
prefix='moo144/tradier/2026-09-03/2026-09-03-ca9d268541/'
keys=[weekly[0],weekly[-1]]+[prefix+x for x in ['manifest.json','summary.json','normalized-events-part-000.ndjson.gz']]
if not (O/'source-followup-plan.json').exists(): (O/'source-followup-plan.json').write_text(json.dumps({'frozen_utc':pd.Timestamp.now(tz='UTC').isoformat(),'keys':keys,'total_round2_reads_including_first_pass':12},indent=2))
out=[]
for k in keys:
 cache=D/k.replace('/','__')
 if cache.exists():
  r=requests.Response();r.status_code=200;r._content=cache.read_bytes()
 else:r=requests.get('https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/'+k,timeout=30)
 a={'key':k,'status':r.status_code,'audited_utc':pd.Timestamp.now(tz='UTC').isoformat(),'hash_basis':'requests-decoded response body; transport compression may be removed'}
 if r.status_code!=200:out.append(a);continue
 raw=r.content;(D/k.replace('/','__')).write_bytes(raw);a.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),last_modified=r.headers.get('Last-Modified'))
 if k.endswith('.csv'):
  f=pd.read_csv(io.BytesIO(raw));a.update(rows=len(f),fields=list(f),missing=f.isna().sum().to_dict())
 elif k.endswith('.gz'):
  rows=[json.loads(x) for x in (gzip.decompress(raw) if raw[:2]==bytes([31,139]) else raw).decode().splitlines()];f=pd.json_normalize(rows);a.update(rows=len(f),fields=list(f),missing=f.isna().sum().to_dict());a['categories']={c:f[c].value_counts().head(30).to_dict() for c in f if any(s in c.lower() for s in ['type','symbol','schema'])};a['time_ranges']={c:{'min':str(f[c].min()),'max':str(f[c].max())} for c in f if any(s in c.lower() for s in ['time','_ts']) and f[c].notna().all()}
 else:a['fields']=list(r.json())
 out.append(a)
(O/'source-followup.json').write_text(json.dumps(out,indent=2,default=str));print(json.dumps(out,indent=2,default=str)[:16000])
