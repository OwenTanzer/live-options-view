import json
from pathlib import Path
import numpy as np,pandas as pd
R=Path(__file__).resolve().parents[2];O=R/'outputs'/'round2';D=R/'work'/'private';P=json.loads((R/'outputs'/'protocol.json').read_text());S=json.loads((O/'protocol.json').read_text())
def load(symbol):
    x=pd.concat([pd.DataFrame(json.loads(f.read_text())) for f in sorted(D.glob('history__'+symbol+'__5__20*.json'))]).sort_values('t')
    assert not x.t.duplicated().any()
    dt=pd.to_datetime(x.t,unit='s',utc=True).dt.tz_convert('America/New_York');x['day']=dt.dt.strftime('%Y-%m-%d');x['slot']=dt.dt.hour*60+dt.dt.minute-570
    return x[x.day.isin(P['split']['discovery']+P['split']['validation'])].copy()
def qqq_panel():
    parts=[]
    for day,g in load('QQQ').groupby('day'):
        g=g[(g.slot>=0)&(g.slot<390)].copy();delta=g.t.diff();g['r1']=np.log(g.c/g.c.shift());g['r6']=np.log(g.c/g.c.shift(6));g['r1sq']=g.r1**2;g['rv6']=g.r1sq.rolling(6).mean();g['rv12']=g.r1sq.rolling(12).mean();g['u']=(g.slot-195)/195;g['u2']=g.u**2;g['tod']=np.cos(2*np.pi*g.slot/390);g['y']=np.log(g.c.shift(-2)/g.c.shift(-1));g['y2']=g.y**2;g['available']=g.t+360
        past=delta.eq(300).rolling(12).sum().eq(12);future=delta.shift(-1).eq(300)&delta.shift(-2).eq(300)
        parts.append(g[past&future].dropna())
    return pd.concat(parts).sort_values('t')
def nvda_predictor(x):
    parts=[]
    for day,g in x.groupby('day'):
        g=g[(g.slot>=0)&(g.slot<390)].copy();delta=g.t.diff();g['nvda_r1']=np.log(g.c/g.c.shift());g['nvda_r1_lag5']=g.nvda_r1.shift();g['available']=g.t+360
        parts.append(g[delta.eq(300)&delta.shift().eq(300)][['t','day','available','nvda_r1','nvda_r1_lag5']])
    return pd.concat(parts).sort_values('available')
def ridge(train,test,features,target):
    X=train[features].to_numpy();mu=X.mean(0) if features else np.array([]);sd=X.std(0) if features else np.array([]);sd[sd==0]=1
    X=np.column_stack([np.ones(len(train)),(X-mu)/sd]);pen=np.eye(X.shape[1]);pen[0,0]=0
    b=np.linalg.solve(X.T@X+pen,X.T@train[target].to_numpy());return np.column_stack([np.ones(len(test)),(test[features]-mu)/sd])@b
def interval(delta):
    delta=np.asarray(delta);rng=np.random.default_rng(S['seed']);n=len(delta);start=rng.integers(0,n,(2000,(n+4)//5));idx=(start[:,:,None]+np.arange(5))%n;b=delta[idx.reshape(2000,-1)[:,:n]].mean(1)
    return float(np.quantile(b,.025)),float(np.quantile(b,.975))
