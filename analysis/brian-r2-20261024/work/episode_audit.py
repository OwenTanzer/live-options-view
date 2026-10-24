import pandas as pd,json,numpy as np
from pathlib import Path
O=Path(__file__).resolve().parents[1]/'outputs'
d=pd.read_csv(O/'daily-losses.csv');d['delta']=d.loss-d.base;d['month']=d.day.str[:7]
m=d.groupby(['model','metric','month'])[['loss','base','delta']].mean().reset_index();m['ratio']=m.loss/m.base;m.to_csv(O/'monthly-losses.csv',index=False)
out=[]
for (model,metric),g in d.groupby(['model','metric']):
    benefit=-g.delta; total=benefit.sum(); top=benefit.nlargest(5)
    out.append({'model':model,'metric':metric,'days_better':int((benefit>0).sum()),'days':len(g),'top5_benefit_share':float(top.sum()/total) if total else None,'difference_excluding_best5':float(g.loc[~g.index.isin(top.index),'delta'].mean()),'difference_excluding_april2025':float(g.loc[g.month!='2025-04','delta'].mean())})
pd.DataFrame(out).to_csv(O/'episode-audit.csv',index=False)
print(pd.DataFrame(out).to_string(index=False))
