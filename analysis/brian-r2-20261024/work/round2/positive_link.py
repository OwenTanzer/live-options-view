from common import *
from scipy.optimize import minimize
import scipy
spec={'frozen_utc':pd.Timestamp.now(tz='UTC').isoformat(),'reason':'Linear additive seasonal banks produced 1/3 negative validation forecasts, making QLIKE extremely floor-sensitive. Preserve that result; examine a separate positive-link family.','family':'log-link conditional second-moment model, QLIKE training objective','banks':S['banks'],'parameters':S['parameter_counts_including_intercept'],'memories':[20,60],'objective':'mean(log(f)+y2/f) + sum(nonintercept standardized coefficients squared)/(2*n); f=exp(X beta)','comparison_limit':'Changes link AND fitting objective relative to linear ridge. Cannot attribute any difference solely to positivity or seasonality across families. Seasonal ablations within this family share objective/link.','initialization':'intercept log(training mean y2), slopes0; no warm-start or model selection','optimizer':'BFGS, analytic gradient, gtol1e-7, maxiter1000; failed fits reported, no silent replacement','no_prediction_floor':True,'mixture':'fixed equal20/60 in variance space','evaluation':'consumed validation only; untouched final remains unscored','seed':S['seed'],'scipy':scipy.__version__}
(O/'positive-protocol.json').write_text(json.dumps(spec,indent=2));print('POSITIVE_FAMILY_FROZEN',flush=True)
d=qqq_panel();records=[];fitlog=[]
for day in P['split']['validation']:
    te=d[d.day==day];previous=sorted(d.loc[d.day<day,'day'].unique());rec=te[['t','day','y2']].copy()
    for memory in [20,60]:
        tr=d[d.day.isin(previous[-memory:])];y=tr.y2.to_numpy();n=len(y)
        for bank,features in S['banks'].items():
            a=tr[features].to_numpy();mu=a.mean(0) if features else np.array([]);sd=a.std(0) if features else np.array([]);sd[sd==0]=1;X=np.column_stack([np.ones(n),(a-mu)/sd]);T=np.column_stack([np.ones(len(te)),(te[features]-mu)/sd]);b0=np.zeros(len(features)+1);b0[0]=np.log(y.mean());pen=np.ones(len(b0));pen[0]=0
            def objective(b):
                eta=X@b
                with np.errstate(over='ignore',invalid='ignore'): ratio=y*np.exp(-eta)
                if not np.isfinite(ratio).all(): return np.inf,np.full(len(b),1e100)
                return float(np.mean(eta+ratio)+np.sum(pen*b*b)/(2*n)),X.T@(1-ratio)/n+pen*b/n
            fit=minimize(objective,b0,jac=True,method='BFGS',options={'gtol':1e-7,'maxiter':1000});value,grad=objective(fit.x);eta=T@fit.x;forecast=np.exp(eta)
            fitlog.append({'day':day,'bank':bank,'memory':memory,'success':bool(fit.success),'message':str(fit.message),'iterations':int(fit.nit),'gradient_max':float(np.max(np.abs(grad))),'forecast_min':float(forecast.min()),'forecast_max':float(forecast.max())});rec[bank+str(memory)]=forecast
    records.append(rec)
p=pd.concat(records);p.to_csv(D/'round2-positive-predictions.csv',index=False);pd.DataFrame(fitlog).to_csv(O/'positive-fit-audit.csv',index=False)
models=list(p.columns[3:])
for bank in S['banks']: p[bank+'_equal']=p[[bank+'20',bank+'60']].mean(axis=1);models.append(bank+'_equal')
out=[];daily=[]
for model in models:
    baseline='unconditional_equal' if model.endswith('_equal') else 'unconditional'+model[-2:]
    for metric in ['mse','qlike']:
        loss=(p.y2-p[model])**2 if metric=='mse' else np.log(p[model])+p.y2/p[model];base=(p.y2-p[baseline])**2 if metric=='mse' else np.log(p[baseline])+p.y2/p[baseline];g=pd.DataFrame({'day':p.day,'loss':loss,'base':base}).groupby('day').mean();lo,hi=interval(g.loss-g.base)
        out.append({'model':model,'metric':metric,'loss':float(loss.mean()),'mse_ratio':float(loss.mean()/base.mean()) if metric=='mse' else None,'daily_difference':float((g.loss-g.base).mean()),'block_lo':lo,'block_hi':hi});g=g.reset_index();g['model']=model;g['metric']=metric;daily.append(g)
pd.DataFrame(out).to_csv(O/'positive-scores.csv',index=False);pd.concat(daily).to_csv(O/'positive-daily.csv',index=False)
print(pd.DataFrame(out).to_string(index=False));print('FIT_FAILURES',sum(not x['success'] for x in fitlog));print('POSITIVE_COMPLETE final_unscored=True')
