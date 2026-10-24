import json,hashlib,sys,os
from pathlib import Path
import pandas as pd
OUT=Path(__file__).resolve().parents[1]/'outputs'; OUT.mkdir(exist_ok=True)
BASE=Path(os.environ.get('BRIAN_SOURCE_DIR',str(OUT.parent/'work'/'private')))
files=sorted(BASE.glob('history__QQQ__5__20*.json'))
timestamps=[]; manifest=[]
for p in files:
    b=p.read_bytes(); d=json.loads(b); timestamps+=d['t']
    manifest.append({'key':p.name.replace('__','/'),'local_source':str(p),'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b),'rows':len(d['t']),'schema':list(d)})
days=sorted(set(pd.to_datetime(timestamps,unit='s',utc=True).tz_convert('America/New_York').strftime('%Y-%m-%d')))
n=len(days); a=int(.6*n); b=int(.8*n)
split={'discovery':days[:a],'validation':days[a:b],'final':days[b:]}
protocol={'frozen_utc':pd.Timestamp.now(tz='UTC').isoformat(),'split':split,'families':['price continuation/reversal','past variance versus time-of-day conditioned variance'],'horizon':'next five-minute close-to-close return; same session consecutive bars only','availability':'archived corrected OHLCV; bar assumed available five minutes after its timestamp plus 60s latency; NOT proven original vintages. At origin use previous completed bar and forecast following complete five-minute interval.','evaluation':'validation only this pass; final outcomes not evaluated','features':'lagged 5m return, 30m return, 30m variance; variance alternative replaces 30m return with slot sin/cos under equal dimension','coefficient_memory_days':[20,60],'refit':'daily, previous sessions only','aggregation':'equal weight of two memory forecasts; no adaptive weights','ridge':1.0,'seed':20261024,'uncertainty':'day-block bootstrap 1000 draws; no iid-bar inference','revision':0,'bounds':'no feature or target clipping; variance predictions floored at 1e-12; MSE and QLIKE separately','holdout_rule':'final not used for fitting or scoring this pass'}
(OUT/'source-manifest.json').write_text(json.dumps(manifest,indent=2)); (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
print({k:(len(v),v[0],v[-1]) for k,v in split.items()})
