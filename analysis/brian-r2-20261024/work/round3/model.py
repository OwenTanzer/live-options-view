import json,hashlib
from pathlib import Path
import numpy as np,pandas as pd
R=Path(__file__).resolve().parents[2];O=R/'outputs/round3';D=R/'work/private';S=json.loads((R/'outputs/round2/round3-plan.json').read_text());P=json.loads((R/'outputs/protocol.json').read_text());F=S['candidate_bank']

def load():
 x=pd.concat([pd.DataFrame(json.loads(f.read_text())) for f in sorted(D.glob('history__QQQ__5__20*.json'))]).sort_values('t').reset_index(drop=True)
 assert not x.t.duplicated().any()
 dt=pd.to_datetime(x.t,unit='s',utc=True).dt.tz_convert('America/New_York');x['day']=dt.dt.strftime('%Y-%m-%d');x['slot']=dt.dt.hour*60+dt.dt.minute-570
 return x[x.day.isin(P['split']['discovery']+P['split']['validation']+S['final_sessions'])].copy()

def features(raw):
 parts=[]
 for day,g in raw.groupby('day'):
  g=g[(g.slot>=0)&(g.slot<390)].copy();delta=g.t.diff();r=np.log(g.c/g.c.shift());g['r1sq']=r**2;g['rv6']=g.r1sq.rolling(6).mean();g['rv12']=g.r1sq.rolling(12).mean();g['feature_cutoff']=g.t+300
  parts.append(g[delta.eq(300).rolling(12).sum().eq(12)][['t','day','slot','feature_cutoff']+F])
 return pd.concat(parts).sort_values('t').reset_index(drop=True)

def labels(raw,extra):
 parts=[];h=2+extra
 for day,g in raw.groupby('day'):
  g=g[(g.slot>=0)&(g.slot<390)].copy();delta=g.t.diff();g['y2']=np.log(g.c.shift(-h)/g.c.shift(-(h-1)))**2
  ok=pd.Series(True,index=g.index)
  for k in range(1,h+1):ok &= delta.shift(-k).eq(300)
  g['available']=g.t+360+extra*300;g['target_start']=g.t+h*300;g['target_end']=g.t+(h+1)*300;g['maturity']=g.target_end+60
  parts.append(g[ok][['t','day','y2','available','target_start','target_end','maturity']])
 return pd.concat(parts).sort_values('t')

def fit(tr,bank):
 cols=F if bank=='state' else [];a=tr[cols].to_numpy();mu=a.mean(0) if cols else np.array([]);sd=a.std(0) if cols else np.array([]);sd[sd==0]=1
 X=np.column_stack([np.ones(len(tr)),(a-mu)/sd]);pen=np.eye(X.shape[1]);pen[0,0]=0;b=np.linalg.solve(X.T@X+pen,X.T@tr.y2.to_numpy())
 return cols,mu,sd,b

def predict(model,te):
 cols,mu,sd,b=model;return np.column_stack([np.ones(len(te)),(te[cols].to_numpy()-mu)/sd])@b

def block_interval(g,weighting):
 # g: ordered session sums/counts of paired loss differences. Resample whole sessions in circular five-day blocks.
 n=len(g);rng=np.random.default_rng(20261024);starts=rng.integers(0,n,(2000,(n+4)//5));ix=((starts[:,:,None]+np.arange(5))%n).reshape(2000,-1)[:,:n]
 sums=g['sum'].to_numpy();counts=g['count'].to_numpy()
 b=(sums/counts)[ix].mean(1) if weighting=='equal_day' else sums[ix].sum(1)/counts[ix].sum(1)
 return np.quantile(b,[.025,.975]).tolist()
