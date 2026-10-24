import pandas as pd,numpy as np,json
from pathlib import Path
import matplotlib;matplotlib.use('Agg')
import matplotlib.pyplot as plt
R=Path(__file__).resolve().parents[1];O=R/'outputs';d=pd.read_csv(O/'daily-losses.csv');rng=np.random.default_rng(20261024)
pairs=[('variance_state60','variance_base60'),('variance_state20','variance_base20'),('variance_base20','variance_base60'),('variance_state20','variance_state60')]
out=[]
for metric in ['mse','qlike']:
    w=d[d.metric==metric].pivot(index='day',columns='model',values='loss')
    for a,b in pairs:
        delta=(w[a]-w[b]).to_numpy();n=len(delta);starts=rng.integers(0,n,(2000,(n+4)//5));idx=(starts[:,:,None]+np.arange(5))%n;boot=delta[idx.reshape(2000,-1)[:,:n]].mean(1)
        out.append({'model':a,'comparator':b,'metric':metric,'days':n,'mean_difference':float(delta.mean()),'five_day_circular_bootstrap_lo':float(np.quantile(boot,.025)),'five_day_circular_bootstrap_hi':float(np.quantile(boot,.975))})
pd.DataFrame(out).to_csv(O/'paired-audit.csv',index=False)
fig,axes=plt.subplots(1,2,figsize=(11,4))
for ax,metric in zip(axes,['mse','qlike']):
    for model in ['variance_base20','variance_base60','variance_state60','variance_persistence']:
        g=d[(d.model==model)&(d.metric==metric)].sort_values('day');ax.plot(pd.to_datetime(g.day),(g.loss-g.base).cumsum(),label=model)
    ax.axhline(0,color='black',lw=.7);ax.set_title(metric.upper()+': cumulative daily loss difference');ax.set_ylabel('Lower favors model over rolling mean');ax.tick_params(axis='x',rotation=30);ax.grid(alpha=.2)
axes[1].legend(fontsize=7);fig.suptitle('QQQ validation only: variance forecasting, 168 sessions');fig.tight_layout();fig.savefig(O/'variance-validation.png',dpi=170)
print(pd.DataFrame(out).to_string(index=False))
