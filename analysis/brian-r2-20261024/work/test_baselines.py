import json,sys,hashlib,platform
from pathlib import Path
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]; O=ROOT/'outputs'
P=json.loads((O/'protocol.json').read_text())
P.update({'revision':1,'revision_reason':'Make pre-run feature dimensions and latency gap precise; no forecast results inspected.', 'features':{'direction_base':['intercept','r1','r6','rv6'],'direction_state':['intercept','r1','r6','r1*sqrt(rv6)'],'variance_base':['intercept','r1_squared','rv6','rv12'],'variance_state':['intercept','r1_squared','rv6','cos(2*pi*session_slot/78)']},'horizon':'5-minute return c[i+2]/c[i+1]. Origin timestamp[i]+6min assumes bar i closes at +5min and is released +1min; target starts +10min and matures +16min. Four-minute gap to target start; no intraday refits.'})
(O/'protocol.json').write_text(json.dumps(P,indent=2))
frames=[]; manifest=json.loads((O/'source-manifest.json').read_text())
for m in manifest:
    p=ROOT/'work'/'private'/m['key'].replace('/','__')
    if not p.exists(): p=Path(m['local_source'])
    raw=p.read_bytes(); assert hashlib.sha256(raw).hexdigest()==m['sha256']
    frames.append(pd.DataFrame(json.loads(raw)))
allbars=pd.concat(frames).sort_values('t'); raw_count=len(allbars)
dupes=int(allbars.t.duplicated().sum()); allbars=allbars.drop_duplicates('t',keep='first')
allbars['dt']=pd.to_datetime(allbars.t,unit='s',utc=True).dt.tz_convert('America/New_York'); allbars['day']=allbars.dt.dt.strftime('%Y-%m-%d')
allbars['slot']=allbars.dt.dt.hour*60+allbars.dt.dt.minute-570
coverage={'raw_rows':raw_count,'duplicate_timestamps':dupes,'days':int(allbars.day.nunique()),'min':str(allbars.dt.min()),'max':str(allbars.dt.max()),'missing':allbars[['t','o','h','l','c','v']].isna().sum().to_dict(),'bars_per_day':allbars.groupby('day').size().value_counts().sort_index().to_dict(),'off_session_rows':int(((allbars.slot<0)|(allbars.slot>=390)).sum()),'invalid_ohlc_rows':int(((allbars.l>allbars.h)|(allbars.c<=0)|(allbars.v<0)).sum()),'schema_version':'unversioned t/o/h/l/c/v JSON; source hashes authoritative','first_available':'not present; historical object upload is not original bar availability','data_kind':'OHLCV aggregates, not prints, classified trades, quotes, depth or order flow','quote_staleness':'not measurable in OHLCV','joint_coverage':'Only QQQ OHLCV materialized; no verified joint options/flow/cross-asset panel.'}
(O/'coverage.json').write_text(json.dumps(coverage,indent=2))
# Never construct or score final-period outcomes.
bars=allbars[allbars.day.isin(P['split']['discovery']+P['split']['validation'])].copy()
panel=[]
for day,g in bars.groupby('day'):
    g=g[(g.slot>=0)&(g.slot<390)].copy(); g['r1']=np.log(g.c/g.c.shift()); g['r6']=np.log(g.c/g.c.shift(6))
    g['rv6']=g.r1.pow(2).rolling(6).mean(); g['rv12']=g.r1.pow(2).rolling(12).mean(); g['r1_squared']=g.r1**2
    g['state']=g.r1*np.sqrt(g.rv6); g['tod']=np.cos(2*np.pi*g.slot/390); g['y']=np.log(g.c.shift(-2)/g.c.shift(-1)); g['yv']=g.y**2
    valid=(g.t.shift(-2)-g.t==600)&(g.t-g.t.shift(12)==3600)
    panel.append(g[valid].dropna())
d=pd.concat(panel); rows=[]
banks={'direction_base':['r1','r6','rv6'],'direction_state':['r1','r6','state'],'variance_base':['r1_squared','rv6','rv12'],'variance_state':['r1_squared','rv6','tod']}
for day in P['split']['validation']:
    test=d[d.day==day]; previous=sorted(d.loc[d.day<day,'day'].unique())
    if test.empty: continue
    rec=test[['day','t','y','yv']].copy()
    for name,features in banks.items():
        target='yv' if name.startswith('variance') else 'y'
        for memory in P['coefficient_memory_days']:
            train=d[d.day.isin(previous[-memory:])]; X=train[features].to_numpy(); mu=X.mean(0); sd=X.std(0); sd[sd==0]=1
            X=np.column_stack([np.ones(len(X)),(X-mu)/sd]); z=train[target].to_numpy(); penalty=np.eye(4); penalty[0,0]=0
            beta=np.linalg.solve(X.T@X+P['ridge']*penalty,X.T@z)
            pred=np.column_stack([np.ones(len(test)),(test[features]-mu)/sd])@beta
            rec[name+str(memory)]=np.maximum(pred,1e-12) if target=='yv' else pred
        rec[name+'_equal']=rec[[name+str(x) for x in P['coefficient_memory_days']]].mean(axis=1)
    rec['zero']=0.; rec['variance_persistence']=test.rv6.to_numpy()
    rec['variance_trainmean']=d[d.day.isin(previous[-60:])].yv.mean()
    rows.append(rec)
pred=pd.concat(rows); summaries=[]; daily=[]; rng=np.random.default_rng(P['seed'])
for col in pred.columns[4:]:
    variance=col.startswith('variance'); y=pred.yv if variance else pred.y; benchmark='variance_trainmean' if variance else 'zero'
    for metric in (['mse','qlike'] if variance else ['mse']):
        loss=(y-pred[col])**2 if metric=='mse' else np.log(pred[col])+y/pred[col]
        base=(y-pred[benchmark])**2 if metric=='mse' else np.log(pred[benchmark])+y/pred[benchmark]
        temp=pd.DataFrame({'day':pred.day,'loss':loss,'base':base}); agg=temp.groupby('day').mean(); delta=(agg.loss-agg.base).to_numpy()
        boot=rng.choice(delta,(1000,len(delta)),replace=True).mean(1)
        summaries.append({'model':col,'metric':metric,'rows':len(y),'days':len(agg),'loss':float(loss.mean()),'relative_loss':float(loss.mean()/base.mean()) if metric=='mse' else None,'daily_mean_difference':float(delta.mean()),'day_bootstrap_lo':float(np.quantile(boot,.025)),'day_bootstrap_hi':float(np.quantile(boot,.975)),'hit_rate':float((np.sign(pred[col])==np.sign(y)).mean()) if not variance and col!='zero' else None})
        agg=agg.reset_index();agg['model']=col;agg['metric']=metric;daily.append(agg)
pd.DataFrame(summaries).to_csv(O/'validation-scores.csv',index=False); pd.concat(daily).to_csv(O/'daily-losses.csv',index=False)
# Derived per-origin forecasts remain private and are excluded from Git.
(ROOT/'work'/'private').mkdir(exist_ok=True); pred.to_csv(ROOT/'work'/'private'/'validation-predictions.csv',index=False)
(O/'environment.json').write_text(json.dumps({'python':sys.version,'numpy':np.__version__,'pandas':pd.__version__,'platform':platform.platform(),'seed':P['seed']},indent=2))
print(pd.DataFrame(summaries).to_string(index=False)); print('BASELINE_TEST_COMPLETE final_unscored=True')
