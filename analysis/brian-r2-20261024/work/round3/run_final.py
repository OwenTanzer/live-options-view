from model import *
import datetime
assert (O/'preflight.json').exists()
assert not (O/'consumption.json').exists(),'Final already consumed; use a fresh private reproduction directory'
started=datetime.datetime.now(datetime.timezone.utc).isoformat();cons={'started_utc':started,'status':'FINAL CONSUMED from this run; no retuning permitted','preflight_sha256':hashlib.sha256((O/'preflight.json').read_bytes()).hexdigest()};(O/'consumption.json').write_text(json.dumps(cons,indent=2))
x=load();feat=features(x);records=[];ledger=[];exposure=[];checks=[];timing=[]
for extra in [0,1]:
 panel=feat.merge(labels(x,extra),on=['t','day'],validate='one_to_one');scenario='primary' if extra==0 else 'delayed';assert (panel.target_start>panel.available).all();assert (panel.maturity>panel.target_end).all()
 for day in S['final_sessions']:
  te=panel[panel.day==day].copy();prior=sorted(panel.loc[panel.day<day,'day'].unique());rec=te[['t','day','y2','available','target_start','target_end','maturity']].copy();rec['scenario']=scenario
  # Rebuild features from a same-day prefix. This does not use target observability to select features.
  cut=int(te.t.iloc[len(te)//2]);prefix=features(x[(x.day<day)|((x.day==day)&(x.t<=cut))]);a=feat[(feat.day==day)&(feat.t<=cut)].reset_index(drop=True);b=prefix[prefix.day==day].reset_index(drop=True);pd.testing.assert_frame_equal(a,b)
  timing.append({'scenario':scenario,'day':day,'first_bar_stamp':int(te.t.min()),'feature_cutoff':int(te.feature_cutoff.min()),'available':int(te.available.min()),'target_start':int(te.target_start.min()),'target_end':int(te.target_end.min()),'label_maturity':int(te.maturity.min())})
  for memory in [20,60]:
   days=prior[-memory:];assert len(days)==memory;tr=panel[panel.day.isin(days)];assert tr.day.max()<day;assert tr.maturity.max()<te.available.min()
   ledger.append({'scenario':scenario,'forecast_day':day,'memory':memory,'train_first':tr.day.min(),'train_last':tr.day.max(),'training_rows':len(tr),'training_max_maturity':int(tr.maturity.max()),'forecast_min_available':int(te.available.min()),'training_prior_final_sessions':sum(d in S['final_sessions'] for d in days)})
   for bank in ['unconditional','state']:
    mod=fit(tr,bank);name=bank+str(memory);rec[name]=predict(mod,te);tp=te[te.t<=cut];pfx=b.set_index('t').loc[tp.t].reset_index();err=float(np.max(np.abs(predict(mod,tp)-predict(mod,pfx))));assert err<1e-15;checks.append({'scenario':scenario,'day':day,'model':name,'prefix_prediction_max_difference':err})
   lo=tr[F].min();hi=tr[F].max();z=(te[F]-tr[F].mean())/tr[F].std(ddof=0);outside=te[F].lt(lo)|te[F].gt(hi)
   rec['outside'+str(memory)]=outside.any(axis=1).to_numpy();rec['max_abs_z'+str(memory)]=z.abs().max(axis=1).to_numpy()
   for feature in F:exposure.append({'scenario':scenario,'day':day,'memory':memory,'feature':feature,'rows':len(te),'below_training_min':int(te[feature].lt(lo[feature]).sum()),'above_training_max':int(te[feature].gt(hi[feature]).sum()),'max_abs_training_z':float(z[feature].abs().max()),'train_min':float(lo[feature]),'train_max':float(hi[feature]),'test_min':float(te[feature].min()),'test_max':float(te[feature].max())})
  records.append(rec)
pred=pd.concat(records);pred.to_csv(D/'round3-predictions.csv',index=False);pd.DataFrame(ledger).to_csv(O/'fit-ledger.csv',index=False);pd.DataFrame(exposure).to_csv(O/'state-exposure.csv',index=False);pd.DataFrame(checks).to_csv(O/'truncation-checks.csv',index=False);pd.DataFrame(timing).to_csv(O/'timing-examples.csv',index=False)
cons.update(completed_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),rows_by_scenario=pred.groupby('scenario').size().to_dict(),days_by_scenario=pred.groupby('scenario').day.nunique().to_dict(),training_max_and_prefix_checks_passed=True);(O/'consumption.json').write_text(json.dumps(cons,indent=2));print(json.dumps(cons,indent=2))
