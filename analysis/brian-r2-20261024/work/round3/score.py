from model import *
p=pd.read_csv(D/'round3-predictions.csv');primary=p[p.scenario=='primary'].copy();delay=p[p.scenario=='delayed'].copy();common=set(primary.t)&set(delay.t);assert set(delay.t)==common
views={'primary_full':primary,'primary_matched':primary[primary.t.isin(common)],'delayed_matched':delay};models=['unconditional20','unconditional60','unconditional_equal','state20','state60','state_equal'];scores=[];daily=[];contrasts=[];cal=[];clips=[];events=[];episodes=[];monthly=[];support=[]
pairs=[('state20','unconditional20'),('state60','unconditional60'),('state_equal','unconditional_equal'),('state20','unconditional60'),('state_equal','unconditional60'),('state20','state60'),('state_equal','state20'),('state_equal','state60')]
for scenario,raw in views.items():
 for memory in [20,60]:support.append({'scenario':scenario,'memory':memory,'rows':len(raw),'outside_any_training_feature_range':int(raw['outside'+str(memory)].sum()),'max_abs_training_z':float(raw['max_abs_z'+str(memory)].max())})
 for floor in S['floor_audit']:
  f=raw.copy()
  for bank in ['state','unconditional']:
   for memory in [20,60]:
    name=bank+str(memory);clips.append({'scenario':scenario,'model':name,'floor':floor,'rows':len(f),'negative':int((f[name]<0).sum()),'floor_bind':int((f[name]<floor).sum()),'minimum_raw':float(f[name].min())})
    for _,row in f[f[name]<floor].iterrows():events.append({'scenario':scenario,'model':name,'floor':floor,'day':row.day,'t':int(row.t),'raw_forecast':row[name],'target':row.y2,'outside_training_range':bool(row['outside'+str(memory)]),'max_abs_training_z':row['max_abs_z'+str(memory)]})
    f[name]=np.maximum(f[name],floor)
   f[bank+'_equal']=f[[bank+'20',bank+'60']].mean(axis=1)
  for model in models:
   c=pd.DataFrame({'day':f.day,'forecast':f[model],'target':f.y2}).groupby('day').agg({'forecast':['mean','sum'],'target':['mean','sum','count']});c.columns=['forecast_mean','forecast_sum','target_mean','target_sum','count']
   for weight in ['prediction_weighted','equal_day']:
    fm=f[model].mean() if weight=='prediction_weighted' else c.forecast_mean.mean();ym=f.y2.mean() if weight=='prediction_weighted' else c.target_mean.mean();cal.append({'scenario':scenario,'model':model,'floor':floor,'weighting':weight,'mean_forecast':fm,'mean_realized_squared_return':ym,'forecast_to_realized':fm/ym,'mean_error':fm-ym})
  for metric in ['mse','qlike']:
   loss=pd.DataFrame({model:(f.y2-f[model])**2 if metric=='mse' else np.log(f[model])+f.y2/f[model] for model in models});loss['day']=f.day
   dm=loss.groupby('day')[models].mean();ds=loss.groupby('day')[models].sum();counts=loss.groupby('day').size()
   for model in models:
    own='unconditional_equal' if model.endswith('_equal') else 'unconditional'+model[-2:]
    for weight in ['prediction_weighted','equal_day']:
     means=loss[models].mean() if weight=='prediction_weighted' else dm.mean();v=means[model];scores.append({'scenario':scenario,'model':model,'floor':floor,'metric':metric,'weighting':weight,'rows':len(f),'days':len(dm),'loss':v,'own_reference':own,'own_reference_loss':means[own],'common_reference':'unconditional60','common_reference_loss':means.unconditional60,'difference_own':v-means[own],'difference_common':v-means.unconditional60,'mse_ratio_own':v/means[own] if metric=='mse' else None,'mse_ratio_common':v/means.unconditional60 if metric=='mse' else None})
    for day in dm.index:daily.append({'scenario':scenario,'floor':floor,'metric':metric,'model':model,'day':day,'rows':int(counts[day]),'mean_loss':dm.loc[day,model],'sum_loss':ds.loc[day,model],'own_reference_loss':dm.loc[day,own],'common_reference_loss':dm.loc[day,'unconditional60']})
    if floor==S['floor']:
     for ref in sorted(set([own,'unconditional60'])):
      benefit=dm[ref]-dm[model];total=benefit.sum();top=benefit.nlargest(5);worst=benefit.nsmallest(5);episodes.append({'scenario':scenario,'metric':metric,'model':model,'reference':ref,'days_better':int((benefit>0).sum()),'days':len(dm),'total_daily_benefit':total,'top5_share_of_net_benefit':top.sum()/total if total>0 else None,'top5_days':';'.join(top.index),'worst5_days':';'.join(worst.index)})
     for month in sorted(set(dm.index.str[:7])):
      mask=dm.index.str.startswith(month);n=counts[mask].sum();monthly.append({'scenario':scenario,'metric':metric,'model':model,'month':month,'days':int(mask.sum()),'rows':int(n),'equal_day_loss':dm.loc[mask,model].mean(),'prediction_weighted_loss':ds.loc[mask,model].sum()/n,'equal_day_common_reference':dm.loc[mask,'unconditional60'].mean(),'prediction_weighted_common_reference':ds.loc[mask,'unconditional60'].sum()/n})
   for a,b in pairs:
    g=pd.DataFrame({'sum':ds[a]-ds[b],'count':counts})
    for weight in ['prediction_weighted','equal_day']:
     lo,hi=block_interval(g,weight);diff=g['sum'].sum()/g['count'].sum() if weight=='prediction_weighted' else (g['sum']/g['count']).mean();contrasts.append({'scenario':scenario,'floor':floor,'metric':metric,'weighting':weight,'model':a,'reference':b,'paired_difference':diff,'block_lo':lo,'block_hi':hi})
for name,records in [('scores',scores),('daily-losses',daily),('paired-contrasts',contrasts),('calibration',cal),('clipping-audit',clips),('episode-concentration',episodes),('monthly',monthly),('support-summary',support)]:pd.DataFrame(records).to_csv(O/(name+'.csv'),index=False)
pd.DataFrame(events,columns=['scenario','model','floor','day','t','raw_forecast','target','outside_training_range','max_abs_training_z']).to_csv(O/'floor-events.csv',index=False)
summary={'primary_rows':len(primary),'delayed_rows':len(delay),'common_rows':len(common),'days':primary.day.nunique(),'final_consumed':True,'negative_events_including_repeated_views_floors':len(events),'scores':'prediction-weighted and equal-day separately; MSE ratios only, QLIKE differences','bootstrap':'2000 circular five-session blocks, seed20261024; fitted forecasts held fixed, no refit inside bootstrap; persistence beyond5sessions and selection uncertainty not fully captured'};(O/'score-summary.json').write_text(json.dumps(summary,indent=2))
print(pd.DataFrame(scores).query('floor==1e-12 and scenario=="primary_full" and weighting=="prediction_weighted"')[['model','metric','loss','mse_ratio_own','mse_ratio_common']].to_string(index=False));print(json.dumps(summary,indent=2))
