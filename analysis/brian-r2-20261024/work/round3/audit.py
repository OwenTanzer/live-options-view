from model import *
import scipy,sys,platform
p=pd.read_csv(D/'round3-predictions.csv');x=load();closes=x.set_index('t').c;feat=features(x);audit=[];independent=[]
for scenario,extra in [('primary',0),('delayed',1)]:
 g=p[p.scenario==scenario];h=2+extra;direct=np.log(closes.loc[g.t+h*300].to_numpy()/closes.loc[g.t+(h-1)*300].to_numpy())**2;err=float(np.max(np.abs(direct-g.y2)));assert err<1e-17
 assert np.isfinite(g[['y2','state20','state60','unconditional20','unconditional60']]).all().all()
 assert not g.t.duplicated().any();assert set(g.day)==set(S['final_sessions'])
 audit.append({'scenario':scenario,'direct_target_max_difference':err,'duplicate_origins':int(g.t.duplicated().sum()),'nonfinite_values':0,'rows':len(g),'days':g.day.nunique()})
 panel=feat.merge(labels(x,extra),on=['t','day'])
 for day in [S['final_sessions'][0],S['final_sessions'][len(S['final_sessions'])//2],S['final_sessions'][-1]]:
  te=panel[panel.day==day];prior=sorted(panel.loc[panel.day<day,'day'].unique())
  for memory in [20,60]:
   tr=panel[panel.day.isin(prior[-memory:])];a=tr[F].to_numpy();mu=a.mean(0);sd=a.std(0);sd[sd==0]=1;X=np.column_stack([np.ones(len(tr)),(a-mu)/sd]);pen=np.diag([0.,1.,1.,1.]);aug=np.vstack([X,pen]);target=np.r_[tr.y2.to_numpy(),np.zeros(4)];beta=np.linalg.lstsq(aug,target,rcond=None)[0];pred=np.column_stack([np.ones(len(te)),(te[F].to_numpy()-mu)/sd])@beta;original=g[g.day==day].set_index('t').loc[te.t,'state'+str(memory)].to_numpy();error=float(np.max(np.abs(pred-original)));assert error<1e-15
   independent.append({'scenario':scenario,'day':day,'memory':memory,'augmented_least_squares_prediction_max_difference':error})
ledger=pd.read_csv(O/'fit-ledger.csv');assert (ledger.train_last<ledger.forecast_day).all();assert (ledger.training_max_maturity<ledger.forecast_min_available).all()
frozen=json.loads((O/'preflight.json').read_text());code_checks={n:hashlib.sha256((R/'work/round3'/n).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==h for n,h in frozen['code'].items()};assert all(code_checks.values())
out={'target_and_row_checks':audit,'independent_augmented_least_squares_checks':independent,'training_day_and_maturity_pass':True,'frozen_code_hashes_unchanged':code_checks,'training_label_safety_min_seconds':int((ledger.forecast_min_available-ledger.training_max_maturity).min()),'prediction_truncation_checks':len(pd.read_csv(O/'truncation-checks.csv')),'python':sys.version,'numpy':np.__version__,'pandas':pd.__version__,'scipy':scipy.__version__,'platform':platform.platform(),'no_model_tuning':True,'final_consumed':True};(O/'implementation-audit.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
