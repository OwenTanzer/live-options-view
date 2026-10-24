from model import *
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
d=pd.read_csv(O/'daily-losses.csv');d=d[d.floor==1e-12];fig,axes=plt.subplots(2,2,figsize=(12,7),sharex=True)
for row,scenario in enumerate(['primary_full','delayed_matched']):
 for col,metric in enumerate(['mse','qlike']):
  ax=axes[row,col]
  for model in ['state20','state60','state_equal']:
   g=d[(d.scenario==scenario)&(d.metric==metric)&(d.model==model)].sort_values('day');ax.plot(pd.to_datetime(g.day),(g.common_reference_loss-g.mean_loss).cumsum(),label=model)
  ax.axhline(0,color='black',lw=.7);ax.set_title(('Primary full coverage' if row==0 else 'Delayed deployment, matched coverage')+' | '+metric.upper());ax.set_ylabel('Cumulative daily mean loss benefit');ax.grid(alpha=.2);ax.legend(fontsize=8)
fig.suptitle('Final-period conditional second-moment forecasts\nCommon reference: unconditional60; positive means lower loss; all eligible days retained',fontsize=12);fig.autofmt_xdate();fig.tight_layout();fig.savefig(O/'cumulative-loss-benefit.png',dpi=160);plt.close(fig)
