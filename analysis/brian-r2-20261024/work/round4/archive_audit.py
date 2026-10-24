from pathlib import Path
import json,hashlib,gzip,requests,concurrent.futures,datetime
R=Path(__file__).resolve().parents[2];O=R/'outputs/round4';D=R/'work/private/round3-source';base='https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/'
plan={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'Two previously inspected dates; HEAD all declared parts; GET indices 65,66,130 per date. No model fitting.','maximum_heads':262,'maximum_gets':6}
assert not (O/'archive-plan.json').exists();(O/'archive-plan.json').write_text(json.dumps(plan,indent=2))
parts=[];days=[]
for f in sorted(D.glob('*manifest-*.json')):
 m=json.loads(f.read_text());p=sorted([a for a in m['artifacts'] if a['key'].endswith('.ndjson.gz')],key=lambda a:a['key']);parts+=p;days.append({'manifest':f.name,'parts':len(p),'unique_keys':len({a['key'] for a in p}),'declared_records':sum(a['records'] for a in p),'declared_bytes':sum(a['bytes'] for a in p),'indices_contiguous':all(f'part-{i:04d}' in a['key'] for i,a in enumerate(p))})
assert len(parts)==262
def head(a):
 try:
  r=requests.head(base+a['key'],timeout=30);return {'key':a['key'],'status':r.status_code,'bytes':int(r.headers.get('Content-Length',-1)),'length_matches':int(r.headers.get('Content-Length',-1))==a['bytes']}
 except Exception as e:return {'key':a['key'],'error':type(e).__name__}
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:heads=list(pool.map(head,parts))
(O/'archive-heads.json').write_text(json.dumps(heads,indent=2));samples=[]
for a in parts:
 if not any(f'part-{i:04d}' in a['key'] for i in [65,66,130]):continue
 r=requests.get(base+a['key'],timeout=30,stream=True);b=r.raw.read(decode_content=False);assert r.status_code==200;(D/a['key'].replace('/','__')).write_bytes(b);sha=hashlib.sha256(b).hexdigest();raw=b
 while raw[:2]==b'\x1f\x8b':raw=gzip.decompress(raw)
 rows=[json.loads(l) for l in raw.splitlines()];receipts=[x.get('collector_receipt_timestamp') for x in rows if x.get('collector_receipt_timestamp')];samples.append({'key':a['key'],'sha256':sha,'hash_matches':sha==a['sha256'],'bytes':len(b),'rows':len(rows),'records_match':len(rows)==a['records'],'receipt_first':min(receipts),'receipt_last':max(receipts),'missing_receipts':len(rows)-len(receipts)})
result={'days':days,'heads':len(heads),'head_200':sum(x.get('status')==200 for x in heads),'length_matches':sum(x.get('length_matches',False) for x in heads),'samples':samples,'ceiling':'HEAD verifies object existence and length, not full byte integrity or feed completeness. Six additional parts hash/record verified; uninspected contents and original market coverage remain unverified.'};(O/'archive-audit.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
