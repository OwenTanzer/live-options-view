import json,hashlib
from pathlib import Path
import pandas as pd,numpy as np
R=Path(__file__).resolve().parents[1];O=R/'outputs';D=R/'work'/'private';P=json.loads((O/'protocol.json').read_text())
a=json.loads((O/'source-manifest.json').read_text());b=json.loads((O/'download-manifest.json').read_text())
assert all(x['sha256']==next(y['sha256'] for y in b if y['key']==x['key']) for x in a)
p=pd.read_csv(D/'validation-predictions.csv');c=pd.read_csv(D/'cross-predictions.csv'); assert not set(p.day)&set(P['split']['final']);assert not set(c.day)&set(P['split']['final']);assert set(p.t)==set(c.t)
bars=pd.concat([pd.DataFrame(json.loads((D/x['key'].replace('/','__')).read_text())) for x in a]).sort_values('t'); bars['day']=pd.to_datetime(bars.t,unit='s',utc=True).dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d');counts=bars.groupby('day').size();full=set(counts[counts==78].index);price=bars.set_index('t').c
expected=np.log(price.reindex(p.t+600).to_numpy()/price.reindex(p.t+300).to_numpy());assert np.allclose(expected,p.y,rtol=1e-9,atol=1e-15)
out=[]
for df,family in [(p,'base'),(c,'cross')]:
    z=df[df.day.isin(full)]
    for col in z.columns:
        if col in ['day','t','y','yv']:continue
        variance=col.startswith('variance');y=z.yv if variance else z.y;ref=z.variance_trainmean if variance else 0
        out.append({'family':family,'model':col,'full_session_days':z.day.nunique(),'rows':len(z),'mse_ratio':float(((y-z[col])**2).mean()/((y-ref)**2).mean())})
pd.DataFrame(out).to_csv(O/'full-session-sensitivity.csv',index=False)
(O/'verification.json').write_text(json.dumps({'current_qqq_bytes_match_preserved_extract':True,'final_prediction_rows':0,'cross_and_baseline_validation_timestamps_identical':True,'target_formula_recomputed':True,'validation_rows':len(p),'validation_days':p.day.nunique(),'full_session_validation_days':len(set(p.day)&full),'first_origin_utc':pd.to_datetime(p.t.iloc[0]+360,unit='s',utc=True).isoformat(),'first_target_start_utc':pd.to_datetime(p.t.iloc[0]+600,unit='s',utc=True).isoformat(),'first_target_end_utc':pd.to_datetime(p.t.iloc[0]+900,unit='s',utc=True).isoformat(),'timing_basis':'assuming stored bar timestamps are starts; provider convention remains to be independently verified','short_session_caveat':'43 rows in eight days; calendar/closing-print convention unresolved; full-session-only sensitivity supplied'},indent=2))
f=O/'options-feasibility.json';d=json.loads(f.read_text());[x.pop('sample',None) for x in d];f.write_text(json.dumps(d,indent=2))
print((O/'verification.json').read_text());print(pd.DataFrame(out).to_string(index=False))
