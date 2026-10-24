from common import *
q=qqq_panel();raw=load('NVDA');n=nvda_predictor(raw)
# Independent truncation check: removing later predictor bars cannot alter existing feature rows.
cut=raw[raw.day==P['split']['validation'][0]].t.iloc[30];prefix=nvda_predictor(raw[raw.t<=cut]);a=n[n.t<=cut].reset_index(drop=True);pd.testing.assert_frame_equal(a,prefix.reset_index(drop=True))
d=pd.merge_asof(q.sort_values('available'),n[['available','nvda_r1','nvda_r1_lag5']],on='available',direction='backward',tolerance=0).dropna(subset=['nvda_r1','nvda_r1_lag5']);records=[]
for day in P['split']['validation']:
    te=d[d.day==day];previous=sorted(d.loc[d.day<day,'day'].unique());rec=te[['day','t','y']].copy()
    for bank,features in S['nvda']['banks'].items():
        for memory in [20,60]:
            tr=d[d.day.isin(previous[-memory:])];rec[bank+str(memory)]=ridge(tr,te,features,'y')
        rec[bank+'_equal']=rec[[bank+'20',bank+'60']].mean(axis=1)
    records.append(rec)
p=pd.concat(records);old=pd.read_csv(D/'cross-predictions.csv');audit={'validation_rows_old':len(old),'validation_rows_new':len(p),'timestamp_set_equal':set(old.t)==set(p.t),'predictor_truncation_invariance_pass':True,'final_rows':len(set(p.day)&set(P['split']['final'])),'actual_current_predictions_max_difference':{}}
for col in ['price20','price60','price_equal','nvda20','nvda60','nvda_equal']:
    newcol=col.replace('nvda','nvda_current');m=old[['t',col]].merge(p[['t',newcol]],on='t',suffixes=('_old','_new'));aa=col+'_old' if col==newcol else col;bb=newcol+'_new' if col==newcol else newcol;audit['actual_current_predictions_max_difference'][col]=float((m[aa]-m[bb]).abs().max())
out=[];daily=[]
for col in p.columns[3:]:
    ref='price_equal' if col.endswith('_equal') else 'price'+col[-2:];loss=(p.y-p[col])**2;base=(p.y-p[ref])**2;zero=p.y**2;g=pd.DataFrame({'day':p.day,'loss':loss,'base':base,'zero':zero}).groupby('day').mean();lo,hi=interval(g.loss-g.base)
    out.append({'model':col,'rows':len(p),'days':p.day.nunique(),'ratio_to_zero':float(loss.mean()/zero.mean()),'ratio_to_matched_price':float(loss.mean()/base.mean()),'paired_daily_diff':float((g.loss-g.base).mean()),'block_lo':lo,'block_hi':hi});g=g.reset_index();g['model']=col;daily.append(g)
audit['stop_rule_triggered']=all(x['ratio_to_zero']>1 for x in out);(O/'nvda-eligibility-audit.json').write_text(json.dumps(audit,indent=2));pd.DataFrame(out).to_csv(O/'nvda-scores.csv',index=False);pd.concat(daily).to_csv(O/'nvda-daily.csv',index=False);p.to_csv(D/'round2-nvda-predictions.csv',index=False)
print(json.dumps(audit,indent=2));print(pd.DataFrame(out).to_string(index=False));print('NVDA_CHECK_COMPLETE')
