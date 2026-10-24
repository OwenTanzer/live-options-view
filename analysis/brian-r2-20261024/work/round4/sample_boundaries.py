from pathlib import Path
import json,gzip,collections,pandas as pd
R=Path(__file__).resolve().parents[2];D=R/'work/private/round3-source';out=[]
for day in ['2026-10-22','2026-10-23']:
 chunks={}
 for i in [65,66]:
  f=next(D.glob(f'*{day}*part-{i:04d}.ndjson.gz'));b=f.read_bytes()
  while b[:2]==b'\x1f\x8b':b=gzip.decompress(b)
  chunks[i]=[json.loads(l) for l in b.splitlines()]
 a,b=chunks[65],chunks[66];r=a+b;tr=[x for x in r if x.get('type')=='timesale'];q=[x for x in r if x.get('type')=='quote'];keys=[(x.get('symbol'),x.get('seq')) for x in tr];ages=[]
 for x in q:
  if x.get('biddate') is not None and x.get('askdate') is not None:
   ages.append(pd.Timestamp(x['collector_receipt_timestamp']).timestamp()*1000-min(float(x['biddate']),float(x['askdate'])))
 out.append({'day':day,'adjacent_indices':[65,66],'rows':len(r),'print_rows':len(tr),'print_duplicate_symbol_seq':len(keys)-len(set(keys)),'quote_rows':len(q),'quote_both_side_timestamps':len(ages),'older_side_age_ms_median':float(pd.Series(ages).median()),'older_side_age_ms_max':max(ages),'receipt_boundary_gap_ms':(pd.Timestamp(b[0]['collector_receipt_timestamp'])-pd.Timestamp(a[-1]['collector_receipt_timestamp'])).total_seconds()*1000,'ceiling':'Repeated symbol/seq may be updates, cancellations or corrections; not dropped. Two adjacent samples cannot prove all-day continuity.'})
(R/'outputs/round4/adjacent-sample-audit.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
