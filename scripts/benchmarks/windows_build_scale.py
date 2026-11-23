"""Offline full builder scale probe; generated local archives only."""
import gzip,json,resource,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,'tests')
from test_oa203_windows_live import _write_day,DAYS,BOUNDS
from market_data.options_scanner import windows_live as live
n=int(sys.argv[1]); start=time.monotonic()
with tempfile.TemporaryDirectory() as td:
 root=Path(td); days=[]
 for day in DAYS[:2]:
  d=_write_day(root,day); days.append(d)
  u=json.loads((d/'universe.json').read_text());u['selected']=[{'underlying':'AAA','expiration':'2026-10-09','additional_expiration':None}];(d/'universe.json').write_text(json.dumps(u))
  entries=[]
  for sweep in (1,2):
   p=d/'sweeps'/f'sweep_{sweep:04d}.jsonl.gz'
   with gzip.open(p,'rt') as f: row=json.loads(f.readline())
   with gzip.open(p,'wt') as f:
    for i in range(n):
     row['symbol']=f'AAA261009C{i:08d}';row['strike']=i/1000
     f.write(json.dumps(row,separators=(',',':'))+'\n')
   t=row['t'];entries.append({'sweep':sweep,'file':p.name,'started_ms':t,'ended_ms':t,'rows':n,'chains':{'AAA':{'status':'ok','fetched_ms':t,'rows':n,'expiration':'2026-10-09'}},'truncated':False})
  (d/'sweeps'/'manifest.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in entries))
 print(json.dumps({'phase':'fixture','contracts':n,'rss_mib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,'seconds':time.monotonic()-start}),flush=True)
 report=live.build_windows(days,{'AAA'},root/'emit',full_universe=True,generated_at='fixed')
 print(json.dumps({'phase':'build','contracts':report['contracts'],'windows':[(w['window_id'],w['evaluated']) for w in report['windows']],'rss_mib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,'seconds':time.monotonic()-start}),flush=True)
