from pathlib import Path
import json,pandas as pd,datetime
R=Path(__file__).resolve().parents[2];O=R/'outputs/round4'
assert not (O/'calendar-plan.json').exists(),'Preserve the registered calendar audit'
(O/'calendar-plan.json').write_text(json.dumps({'registered_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'Post-final exploratory calendar inventory only; identify final targets beyond Dec 24 2025 13:00 ET. No rescoring, refitting or model repair.'},indent=2))
frames=[]
for f in (R/'work/private').glob('history__QQQ__5__20*.json'):
 d=json.loads(f.read_text());frames.append(pd.DataFrame({k:d[k] for k in ['t','o','h','l','c','v']}))
b=pd.concat(frames).sort_values('t');b['et']=pd.to_datetime(b.t,unit='s',utc=True).dt.tz_convert('America/New_York');b['day']=b.et.dt.strftime('%Y-%m-%d');short=[]
for day,g in b.groupby('day'):
 if len(g)<78:
  last=g.iloc[-1];short.append({'day':day,'bars':len(g),'first':str(g.et.iloc[0]),'last':str(last.et),'last_ohlc_all_equal':last.o==last.h==last.l==last.c,'last_volume_share':float(last.v/g.v.sum())})
pd.DataFrame(short).to_csv(O/'short-session-inventory.csv',index=False)
p=pd.read_csv(R/'work/private/round3-predictions.csv');cut=int(pd.Timestamp('2025-12-24 13:00',tz='America/New_York').timestamp());affected=p[(p.day=='2025-12-24')&(p.target_end>cut)].copy();affected[['scenario','day','t','available','target_start','target_end','maturity']].to_csv(O/'calendar-boundary-origins.csv',index=False)
result={'short_sessions':len(short),'affected_final_origins':affected.groupby('scenario').size().to_dict(),'interpretation':'Under frozen bar-start convention the 13:00 bar implies a target ending 13:05 after regular equity close. Closing-auction/provider boundary semantics unresolved; historical data have no availability stamps. Published scores unchanged. Earlier short days also entered training; this inventory is not a complete calendar-aware refit.'};(O/'calendar-audit.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
