from common import *
d=qqq_panel();records=[];fits=[]
for day in P['split']['validation']:
    te=d[d.day==day];previous=sorted(d.loc[d.day<day,'day'].unique());rec=te[['t','day','y2']].copy()
    for memory in S['memories_sessions']:
        tr=d[d.day.isin(previous[-memory:])];assert tr.day.max()<day;assert len(previous[-memory:])==memory
        for bank,features in S['banks'].items():
            rec[bank+str(memory)]=ridge(tr,te,features,'y2');fits.append({'forecast_day':day,'bank':bank,'memory':memory,'training_rows':len(tr),'training_first':tr.day.min(),'training_last':tr.day.max(),'parameters':len(features)+1})
    records.append(rec)
p=pd.concat(records);assert not set(p.day)&set(P['split']['final']);old=pd.read_csv(D/'validation-predictions.csv');assert set(p.t)==set(old.t)
p.to_csv(D/'round2-factorial-raw.csv',index=False);pd.DataFrame(fits).to_csv(O/'fit-ledger.csv',index=False)
scores=[];audit=[];daily=[]
for floor in S['floors']:
    f=p.copy();models=list(p.columns[3:]);f[models]=np.maximum(f[models],floor)
    for bank in S['banks']:
        name=bank+'_equal';f[name]=f[[bank+str(m) for m in S['memories_sessions']]].mean(axis=1);models.append(name)
    for model in models:
        if model in p:
            audit.append({'model':model,'floor':floor,'rows':len(p),'negative_count':int((p[model]<0).sum()),'floor_bind_count':int((p[model]<floor).sum()),'minimum_raw':float(p[model].min())})
        baseline='unconditional'+('20' if model.endswith('20') else '60') if not model.endswith('_equal') else 'unconditional_equal'
        for metric in ['mse','qlike']:
            loss=(f.y2-f[model])**2 if metric=='mse' else np.log(f[model])+f.y2/f[model]
            bl=(f.y2-f[baseline])**2 if metric=='mse' else np.log(f[baseline])+f.y2/f[baseline]
            g=pd.DataFrame({'day':f.day,'loss':loss,'base':bl}).groupby('day').mean();delta=g.loss-g.base;lo,hi=interval(delta)
            scores.append({'model':model,'floor':floor,'metric':metric,'rows':len(f),'days':len(g),'loss':float(loss.mean()),'matched_baseline':baseline,'mse_ratio':float(loss.mean()/bl.mean()) if metric=='mse' else None,'daily_difference':float(delta.mean()),'block_lo':lo,'block_hi':hi})
            g=g.reset_index();g['model']=model;g['floor']=floor;g['metric']=metric;daily.append(g)
pd.DataFrame(scores).to_csv(O/'factorial-scores.csv',index=False);pd.DataFrame(audit).to_csv(O/'clipping-audit.csv',index=False);daily=pd.concat(daily);daily.to_csv(O/'factorial-daily.csv',index=False)
old_audit=[{'model':c,'rows':len(old),'saved_at_floor':int((old[c]<=1e-12).sum()),'below_1e10':int((old[c]<1e-10).sum())} for c in old if c.startswith('variance')];pd.DataFrame(old_audit).to_csv(O/'round1-floor-audit.csv',index=False)
contrasts=[];episodes=[]
for metric in ['mse','qlike']:
    z=daily[(daily.floor==S['primary_floor'])&(daily.metric==metric)];w=z.pivot(index='day',columns='model',values='loss')
    pairs=[('state_season'+str(m),'state'+str(m),'season_added_to_state') for m in [20,60]]+[('state_season'+str(m),'season'+str(m),'state_added_to_season') for m in [20,60]]+[(b+'20',b+'60','short_minus_long') for b in S['banks']]
    for a,b,label in pairs:
        delta=w[a]-w[b];lo,hi=interval(delta);contrasts.append({'contrast':label,'model':a,'comparator':b,'metric':metric,'daily_difference':float(delta.mean()),'block_lo':lo,'block_hi':hi})
    interaction=(w.state_season20-w.state_season60)-(w.state20-w.state60);lo,hi=interval(interaction);contrasts.append({'contrast':'season_by_memory_interaction','model':'(state_season20-state_season60)','comparator':'(state20-state60)','metric':metric,'daily_difference':float(interaction.mean()),'block_lo':lo,'block_hi':hi})
    for model,g in z.groupby('model'):
        benefit=g.base-g.loss;total=benefit.sum();top=benefit.nlargest(5);worst=benefit.nsmallest(5)
        episodes.append({'model':model,'metric':metric,'days_better':int((benefit>0).sum()),'days':len(g),'total_daily_benefit':float(total),'top5_share':float(top.sum()/total) if total>0 else None,'top5_days':';'.join(g.loc[top.index,'day']),'worst5_days':';'.join(g.loc[worst.index,'day'])})
pd.DataFrame(contrasts).to_csv(O/'factorial-contrasts.csv',index=False);pd.DataFrame(episodes).to_csv(O/'episode-concentration.csv',index=False)
daily['month']=daily.day.str[:7];daily.groupby(['model','metric','floor','month'])[['loss','base']].mean().reset_index().to_csv(O/'factorial-monthly.csv',index=False)
print(pd.DataFrame(scores).query('floor==1e-12')[['model','metric','loss','mse_ratio']].to_string(index=False));print(pd.DataFrame(audit).query('floor==1e-12').to_string(index=False));print(pd.DataFrame(contrasts).to_string(index=False));print('FACTORIAL_COMPLETE final_unscored=True')
