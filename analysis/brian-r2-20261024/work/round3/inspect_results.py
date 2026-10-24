from model import *
for name,cols in [('scores',['scenario','model','metric','weighting','loss','mse_ratio_own','mse_ratio_common']),('paired-contrasts',None),('calibration',None),('episode-concentration',None)]:
 d=pd.read_csv(O/(name+'.csv'))
 if 'floor' in d:d=d[d.floor==1e-12]
 if name=='scores':d=d[d.weighting=='prediction_weighted']
 elif name=='paired-contrasts':d=d[(d.scenario=='primary_full')&(d.weighting=='prediction_weighted')]
 elif name=='calibration':d=d[(d.scenario=='primary_full')&(d.weighting=='prediction_weighted')]
 elif name=='episode-concentration':d=d[(d.scenario=='primary_full')&(d.reference=='unconditional60')&d.model.str.startswith('state')]
 print('\n'+name);print(d[cols].to_string(index=False) if cols else d.to_string(index=False))
print('\nFLOOR_SENSITIVITY');d=pd.read_csv(O/'scores.csv');print(d[(d.scenario=='primary_full')&(d.weighting=='prediction_weighted')&(d.metric=='qlike')][['model','floor','loss']].to_string(index=False))
