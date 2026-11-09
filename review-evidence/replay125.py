import sys,json,hashlib,requests,csv,io,subprocess,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from market_data.options_scanner.outputs import build_contract_rows
from market_data.options_scanner.returns import ReturnPolicy,QualifiedPolicy,QUALIFIED
BASE='https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/oa203/scanner/'
OUT=Path(__file__).parent
CACHE=ROOT.parent/'chej-review125-cache'
DAYS=['2026-09-28','2026-09-29','2026-09-30','2026-10-01','2026-10-02','2026-10-05','2026-10-06','2026-10-07']
def run(day):
 d=CACHE/day;d.mkdir(parents=True,exist_ok=True); receipts=[]
 def get(key):
  p=d/key;p.parent.mkdir(parents=True,exist_ok=True)
  if not p.exists():
   r=requests.get(BASE+day+'/'+key,timeout=60);r.raise_for_status();p.write_bytes(r.content)
  b=p.read_bytes();rec={'key':day+'/'+key,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()};receipts.append(rec);return b
 summary=json.loads(get('summary.json'))
 manifest=[json.loads(x) for x in get('sweeps/manifest.jsonl').decode().splitlines() if x.strip()]
 legacy=list(csv.DictReader(io.StringIO(get('leaderboard.csv').decode())))
 keys=['sweeps/sweep_%04d.jsonl.gz'%m['sweep'] for m in manifest]
 with ThreadPoolExecutor(max_workers=4) as ex:list(ex.map(get,keys))
 print(day,'downloaded',len(keys),'sweeps',flush=True)
 damaged=[]
 rows=build_contract_rows(d,ReturnPolicy(**{k:v for k,v in summary['return_policy'].items() if k!='version'}),summary['session_open_ms'],summary['session_close_ms'],damaged=damaged)
 by={r['symbol']:r for r in rows}
 mismatches=[]
 fields=['mid_first_to_max_pct','exec_first_to_max_pct','rank','clean_rank','clean']
 for old in legacy:
  for f in fields:
   a=old.get(f);b=by.get(old['symbol'],{}).get(f)
   if a in ('','None',None) and b is None:continue
   if str(a)==str(b):continue
   try:
    if abs(float(a)-float(b))<1e-6:continue
   except (TypeError,ValueError):pass
   mismatches.append([old['symbol'],f,a,b])
 eligible=[r for r in rows if r.get(QUALIFIED+'_status')=='eligible']
 oldset={r['symbol'] for r in legacy}
 top=sorted(eligible,key=lambda r:r[QUALIFIED+'_rank'])[:200]
 controls=[{k:v for k,v in r.items() if k in ('symbol','clean','rank','clean_rank','mid_first_to_max_pct','exec_first_to_max_pct') or k.startswith(QUALIFIED)} for r in rows if r['underlying'] in ('SOXL','GS','MU','SPCX') and (r.get('clean_rank') and r['clean_rank']<=20 or r['symbol'] in ('GS261002P00600000','GS261009C01160000','GS261009C00940000'))]
 result={'day':day,'revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'sweeps':len(keys),'contracts':len(rows),'statuses':dict(Counter(r.get(QUALIFIED+'_status') for r in rows)),'reasons':dict(Counter(reason for r in rows for reason in r.get(QUALIFIED+'_reasons',[]))),'qualified_negative_or_zero':sum(r[QUALIFIED+'_pct']<=0 for r in eligible),'qualified_top200_outside_legacy':sum(r['symbol'] not in oldset for r in top),'legacy_rows_compared':len(legacy),'legacy_mismatches':mismatches,'damaged':damaged,'top10':[{'symbol':r['symbol'],'qualified_rank':r[QUALIFIED+'_rank'],'qualified_return':r[QUALIFIED+'_pct'],'legacy_rank':r['rank'],'clean_rank':r['clean_rank']} for r in top[:10]],'controls':controls,'sources':sorted(receipts,key=lambda x:x['key'])}
 (OUT/(day+'.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
 print(day,'DONE',result['statuses'],'mismatches',len(mismatches),'damaged',len(damaged),flush=True)
 return {k:v for k,v in result.items() if k not in ('sources','controls','reasons','top10')}
if __name__=='__main__':
 with ThreadPoolExecutor(max_workers=2) as ex:results=list(ex.map(run,DAYS))
 (OUT/'summary125.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
 print('ALL COMPLETE',flush=True)
