import sys,tempfile,time,resource,json
from pathlib import Path
from market_data.options_scanner.windows_spool import WindowSpool,BuildLimits
n=int(sys.argv[1]);start=time.monotonic()
if len(sys.argv) > 2:
 resource.setrlimit(resource.RLIMIT_AS, (int(sys.argv[2])*2**20,)*2)
row=dict(underlying='AAA',type='call',strike=100,expiration='2026-11-13',contract_size=100,root='AAA',t=1794316200000,sweep=1,bid=.1,ask=.2,bid_ms=1794316200000,ask_ms=1794316200000,spot=100,bid_size=10,ask_size=10,volume=5,oi=100)
with tempfile.TemporaryDirectory() as td:
 s=WindowSpool(Path(td)/'rows.sqlite',BuildLimits())
 try:
  for i in range(n):s[f'AAA261113C{i%10000:08d}'].append(row)
  s.db.commit()
  assert sum(len(rows) for symbol,rows in s.items())==n
  print(json.dumps(dict(rows=n,symbols=len(s.ids),rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,spool_mib=s.path.stat().st_size/2**20,seconds=time.monotonic()-start)))
 finally:s.close()
