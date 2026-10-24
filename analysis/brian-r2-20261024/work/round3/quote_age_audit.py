from pathlib import Path
import gzip,json,pandas as pd
R=Path(__file__).resolve().parents[2];D=R/'work/private/round3-source';O=R/'outputs/round3';out=[]
for path in sorted(D.glob('*part-0000*')):
 f=pd.DataFrame([json.loads(l) for l in gzip.decompress(path.read_bytes()).decode().splitlines()]);q=f[f.type=='quote'].copy();receipt=pd.to_datetime(q.collector_receipt_timestamp,utc=True);ages={}
 for side in ['bid','ask']:
  ts=pd.to_numeric(q[side+'date'],errors='coerce');ages[side]=(receipt-pd.to_datetime(ts,unit='ms',utc=True,errors='coerce')).dt.total_seconds()*1000
 age=pd.DataFrame(ages);oldest=age.max(axis=1);out.append({'source_key':path.name.replace('__','/'),'quotes':len(q),'invalid_side_timestamps':age.isna().sum().to_dict(),'negative_side_age_counts':age.lt(0).sum().to_dict(),'oldest_side_age_ms':oldest.quantile([0,.5,.95,1]).to_dict(),'oldest_side_over_1000ms':int(oldest.gt(1000).sum()),'oldest_side_over_5000ms':int(oldest.gt(5000).sum()),'zero_bid':int(pd.to_numeric(q.bid).eq(0).sum()),'crossed':int((pd.to_numeric(q.bid)>pd.to_numeric(q.ask)).sum()),'note':'Age at receipt, not at a later trade. A later matched research join must use preceding receipt and update age to its feature cutoff; no future quote join.'})
(O/'quote-side-age-audit.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
