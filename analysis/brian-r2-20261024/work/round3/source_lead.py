import requests,json,hashlib
from pathlib import Path
from datetime import datetime,timezone
R=Path(__file__).resolve().parents[2];O=R/'outputs/round3';D=R/'work/private/round3-source';D.mkdir(parents=True,exist_ok=True)
keys=[f'moo144/tradier/{day}/{name}' for day in ['2026-10-22','2026-10-23'] for name in ['health.json','universe.json']]
plan={'registered_utc':datetime.now(timezone.utc).isoformat(),'keys':keys,'basis':'Existing scripts/moo144_tradier_collector.py QQQ_ARCHIVE_ROOT and Lane prefix/health/universe paths at inspected revision26332bb0eba41618f4a8b941e74ff202c66b0fe0','purpose':'read-only existence and schema probe, not collection or model fitting'}
assert not (O/'source-lead-plan.json').exists();(O/'source-lead-plan.json').write_text(json.dumps(plan,indent=2));out=[]
for key in keys:
 r=requests.get('https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/'+key,timeout=20);rec={'key':key,'status':r.status_code,'retrieved_utc':datetime.now(timezone.utc).isoformat()}
 if r.status_code==200:
  raw=r.content;(D/key.replace('/','__')).write_bytes(raw);v=r.json();rec.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),fields=list(v),last_modified=r.headers.get('Last-Modified'))
  rec['selected_metadata']={k:v[k] for k in ['run_id','underlying','run_date','date','status','first_receipt_timestamp','last_receipt_timestamp','event_counts','coverage','symbols'] if k in v}
 out.append(rec)
(O/'source-lead.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
