import json,hashlib
from pathlib import Path
import pandas as pd,numpy as np
R=Path(__file__).resolve().parents[1]; O=R/'outputs'; D=R/'work'/'private'; P=json.loads((O/'protocol.json').read_text())
spec={'frozen_utc':pd.Timestamp.now(tz='UTC').isoformat(),'family':'NVDA to QQQ lead-lag','selection':'NVDA chosen from confirmed available manifest before downloading outcome tests; not optimized among assets','target':P['horizon'],'memories':[20,60],'banks':{'price':['r1','rv6','tod','r6'],'nvda':['r1','rv6','tod','nvda_r1']},'coefficients':5,'join':'backward as-of on assumed bar availability timestamp t+360 seconds; tolerance 0 seconds; require exact common completed bar; lagged features require consecutive session bars','evaluation':'same frozen validation, final unscored; validation already consumed by price/variance work','aggregation':'equal 20/60 mixture','search_count':'4 fitted streams and 2 mixtures; no optimized hyperparameters'}
(O/'cross-protocol.json').write_text(json.dumps(spec,indent=2))
data={};cov={}
for symbol in ['QQQ','NVDA']:
    files=sorted(D.glob('history__'+symbol+'__5__20*.json')); x=pd.concat([pd.DataFrame(json.loads(p.read_text())) for p in files]).sort_values('t')
    dt=pd.to_datetime(x.t,unit='s',utc=True).dt.tz_convert('America/New_York');x['day']=dt.dt.strftime('%Y-%m-%d');x['slot']=dt.dt.hour*60+dt.dt.minute-570
    cov[symbol]={'rows':len(x),'days':int(x.day.nunique()),'first':str(dt.min()),'last':str(dt.max()),'duplicate_timestamps':int(x.t.duplicated().sum()),'missing':x[['t','o','h','l','c','v']].isna().sum().to_dict()}
    data[symbol]=x
common=np.intersect1d(data['QQQ'].t,data['NVDA'].t); dt=pd.to_datetime(common,unit='s',utc=True).tz_convert('America/New_York')
cov['actual_intersection']={'timestamps':len(common),'days':len(set(dt.date)),'first':str(dt.min()),'last':str(dt.max()),'cadence_seconds':300,'availability':'assumed equal 60s bar release delay, not original vintage proof'}
(O/'cross-coverage.json').write_text(json.dumps(cov,indent=2))
for symbol,x in data.items():
    x=x[x.day.isin(P['split']['discovery']+P['split']['validation'])].copy(); parts=[]
    for day,g in x.groupby('day'):
        g=g[(g.slot>=0)&(g.slot<390)].copy();g['r1']=np.log(g.c/g.c.shift());g['r6']=np.log(g.c/g.c.shift(6));g['rv6']=g.r1.pow(2).rolling(6).mean();g['tod']=np.cos(2*np.pi*g.slot/390);g['y']=np.log(g.c.shift(-2)/g.c.shift(-1));g['available']=g.t+360
        valid=(g.t-g.t.shift(12)==3600)&(g.t.shift(-2)-g.t==600);parts.append(g[valid].dropna())
    data[symbol]=pd.concat(parts).sort_values('available')
d=pd.merge_asof(data['QQQ'],data['NVDA'][['available','r1']].rename(columns={'r1':'nvda_r1'}),on='available',direction='backward',tolerance=0).dropna()
rows=[]
for day in P['split']['validation']:
    te=d[d.day==day];prior=sorted(d.loc[d.day<day,'day'].unique())
    if te.empty:continue
    rec=te[['day','t','y']].copy()
    for name,features in spec['banks'].items():
        for memory in spec['memories']:
            tr=d[d.day.isin(prior[-memory:])];X=tr[features].to_numpy();mu=X.mean(0);sd=X.std(0);sd[sd==0]=1;X=np.column_stack([np.ones(len(X)),(X-mu)/sd]);pen=np.eye(5);pen[0,0]=0
            beta=np.linalg.solve(X.T@X+pen,X.T@tr.y.to_numpy());rec[name+str(memory)]=np.column_stack([np.ones(len(te)),(te[features]-mu)/sd])@beta
        rec[name+'_equal']=rec[[name+str(v) for v in spec['memories']]].mean(axis=1)
    rows.append(rec)
pred=pd.concat(rows);out=[];rng=np.random.default_rng(P['seed']);daily=[]
for col in pred.columns[3:]:
    base=col.replace('nvda','price');loss=(pred.y-pred[col])**2; baseline=(pred.y-pred[base])**2
    g=pd.DataFrame({'day':pred.day,'loss':loss,'base':baseline}).groupby('day').mean();delta=(g.loss-g.base).to_numpy();boot=rng.choice(delta,(1000,len(delta)),replace=True).mean(1)
    out.append({'model':col,'rows':len(pred),'days':len(g),'mse':float(loss.mean()),'ratio_to_zero':float(loss.mean()/pred.y.pow(2).mean()),'ratio_to_matched_price':float(loss.mean()/baseline.mean()),'day_difference':float(delta.mean()),'bootstrap_lo':float(np.quantile(boot,.025)),'bootstrap_hi':float(np.quantile(boot,.975)),'sign_hit_rate':float((np.sign(pred[col])==np.sign(pred.y)).mean())});g=g.reset_index();g['model']=col;daily.append(g)
pd.DataFrame(out).to_csv(O/'cross-scores.csv',index=False);pd.concat(daily).to_csv(O/'cross-daily-losses.csv',index=False);pred.to_csv(D/'cross-predictions.csv',index=False)
print(json.dumps(cov,indent=2));print(pd.DataFrame(out).to_string(index=False));print('CROSS_TEST_COMPLETE final_unscored=True')
