from pathlib import Path
import csv,json,math,collections
import numpy as np,pandas as pd
R=Path(__file__).resolve().parents[2];O=R/'outputs/round4';O.mkdir(exist_ok=True);D=R/'work/private'
rows=list(csv.DictReader((D/'round3-predictions.csv').open()));groups={s:[r for r in rows if r['scenario']==s] for s in ['primary','delayed']};shared={r['t'] for r in groups['delayed']};views={'primary_full':groups['primary'],'primary_matched':[r for r in groups['primary'] if r['t'] in shared],'delayed_matched':groups['delayed']};models=['unconditional20','unconditional60','unconditional_equal','state20','state60','state_equal'];out=[];events=[];mix=[];weights=[]
expected={tuple(r[k] for k in ['scenario','model','floor','metric','weighting']):r for r in csv.DictReader((R/'outputs/round3/scores.csv').open())};max_error=0.
for scenario,rs in views.items():
 counts=collections.Counter(r['day'] for r in rs)
 for day,n in sorted(counts.items()):weights.append({'scenario':scenario,'day':day,'rows':n,'row_weight':n/len(rs),'equal_day_weight':1/len(counts)})
 for floor in [1e-14,1e-12,1e-10]:
  losses={m:{'mse':[],'qlike':[]} for m in models};byday={m:{k:collections.defaultdict(list) for k in ['mse','qlike']} for m in models};fm={m:[] for m in models};target=[];mixdiff=[]
  for r in rs:
   y=float(r['y2']);target.append(y);forecast={m:max(float(r[m]),floor) for m in models if not m.endswith('_equal')}
   for bank in ['state','unconditional']:forecast[bank+'_equal']=(forecast[bank+'20']+forecast[bank+'60'])/2
   alternative=max((float(r['state20'])+float(r['state60']))/2,floor);mixdiff.append(forecast['state_equal']-alternative)
   for m in models:
    f=forecast[m];mse=(y-f)*(y-f);qlike=math.log(f)+y/f;fm[m].append(f)
    for metric,v in [('mse',mse),('qlike',qlike)]:losses[m][metric].append(v);byday[m][metric][r['day']].append(v)
    if floor==1e-12 and (m.startswith('state')) and (float(r['state20'])<floor or float(r['state60'])<floor):events.append({'scenario':scenario,'day':r['day'],'t':int(r['t']),'model':m,'raw20':float(r['state20']),'raw60':float(r['state60']),'scored_forecast':f,'target':y,'log_forecast':math.log(f),'target_over_forecast':y/f,'qlike':qlike,'qlike_contribution_to_total_mean':qlike/len(rs),'mse':mse,'mse_contribution_to_total_mean':mse/len(rs)})
  mix.append({'scenario':scenario,'floor':floor,'frozen_rule':'mean(max(f20,floor),max(f60,floor))','different_from_clip_after_mix_count':sum(abs(v)>0 for v in mixdiff),'maximum_difference_from_clip_after_mix':max(abs(v) for v in mixdiff)})
  for metric in ['mse','qlike']:
   for weighting in ['prediction_weighted','equal_day']:
    means={m:math.fsum(losses[m][metric])/len(rs) if weighting=='prediction_weighted' else math.fsum(math.fsum(v)/len(v) for v in byday[m][metric].values())/len(counts) for m in models}
    for m in models:
     ref='unconditional_equal' if m.endswith('_equal') else 'unconditional'+m[-2:];old=expected[(scenario,m,str(floor),metric,weighting)];value=means[m];err=abs(value-float(old['loss']));max_error=max(max_error,err);assert math.isclose(value,float(old['loss']),rel_tol=3e-13,abs_tol=1e-24)
     assert math.isclose(means['unconditional60'],float(old['common_reference_loss']),rel_tol=3e-13,abs_tol=1e-24)
     assert math.isclose(means[ref],float(old['own_reference_loss']),rel_tol=3e-13,abs_tol=1e-24)
     assert math.isclose(value-means[ref],float(old['difference_own']),rel_tol=3e-10,abs_tol=1e-24)
     assert math.isclose(value-means['unconditional60'],float(old['difference_common']),rel_tol=3e-10,abs_tol=1e-24)
     if metric=='mse':
      assert math.isclose(value/means[ref],float(old['mse_ratio_own']),rel_tol=3e-13);assert math.isclose(value/means['unconditional60'],float(old['mse_ratio_common']),rel_tol=3e-13)
     out.append({'scenario':scenario,'model':m,'floor':floor,'metric':metric,'weighting':weighting,'independent_loss':value,'published_loss':float(old['loss']),'absolute_difference':err,'own_control':ref,'own_control_loss':means[ref],'common_control_loss':means['unconditional60']})
for name,data in [('independent-scores',out),('june-event-arithmetic',events),('mixture-construction',mix),('row-day-weights',weights)]:pd.DataFrame(data).to_csv(O/(name+'.csv'),index=False)
# Independently reconstruct feature arrays and solve the June fits via augmented least squares, not normal equations or imported model functions.
raw=[]
for f in sorted(D.glob('history__QQQ__5__20*.json')):
 d=json.loads(f.read_text());raw.extend(zip(d['t'],d['c']))
daily=collections.defaultdict(list)
for t,c in sorted(raw):
 stamp=pd.Timestamp(t,unit='s',tz='UTC').tz_convert('America/New_York')
 if 570<=stamp.hour*60+stamp.minute<960:daily[stamp.strftime('%Y-%m-%d')].append((t,c))
fitchecks=[]
for extra in [0,1]:
 panel={};h=2+extra
 for day,seq in daily.items():
  t=np.array([v[0] for v in seq]);c=np.array([v[1] for v in seq]);r=np.r_[np.nan,np.diff(np.log(c))];part=[]
  for i in range(12,len(seq)-h):
   if not np.all(np.diff(t[i-12:i+h+1])==300):continue
   # log(c_i/c_prev) matches declared primitive; np.diff(log c) is not substituted in the fit.
   sq=np.array([math.log(c[j]/c[j-1])**2 for j in range(i-11,i+1)]);part.append((int(t[i]),[sq[-1],math.fsum(sq[-6:])/6,math.fsum(sq)/12],math.log(c[i+h]/c[i+h-1])**2))
  if part:panel[day]=part
 scenario='primary' if extra==0 else 'delayed';actual={int(r['t']):r for r in groups[scenario]}
 for day in ['2026-06-10','2026-06-11']:
  for memory in [20,60]:
   days=[d for d in sorted(panel) if d<day][-memory:];tr=[r for d in days for r in panel[d]];te=panel[day];X=np.array([r[1] for r in tr]);mu=X.mean(0);sd=X.std(0);sd[sd==0]=1;Z=np.column_stack([np.ones(len(tr)),(X-mu)/sd]);aug=np.vstack([Z,np.diag([0.,1.,1.,1.])]);y=np.r_[[r[2] for r in tr],np.zeros(4)];b=np.linalg.lstsq(aug,y,rcond=None)[0];pred=np.column_stack([np.ones(len(te)),(np.array([r[1] for r in te])-mu)/sd])@b
   for item,f in zip(te,pred):
    original=float(actual[item[0]]['state'+str(memory)]);assert abs(f-original)<1e-15
    if f<0:fitchecks.append({'scenario':scenario,'day':day,'t':item[0],'model':'state'+str(memory),'independent_forecast':f,'published_raw_forecast':original,'difference':f-original,'training_first':days[0],'training_last':days[-1],'coefficients_standardized':b.tolist()})
(O/'independent-june-fits.json').write_text(json.dumps(fitchecks,indent=2));summary={'score_rows_checked':len(out),'all_passed':True,'maximum_absolute_score_difference':max_error,'independently_rebuilt_negative_forecasts':len(fitchecks),'scoring_method':'Python csv/math.fsum with explicit per-day groups; published scoring module not imported','fit_method':'Raw closes, independent Python feature loops, augmented least squares; no retuning','final_consumed':True};(O/'arithmetic-summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2));print(pd.DataFrame(events).query('scenario=="primary_full"').to_string(index=False))
