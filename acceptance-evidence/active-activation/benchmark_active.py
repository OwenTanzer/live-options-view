import subprocess,sys,json,time,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'scripts'))
from oa203_active_budget import constrain_builder
root=Path('/home/owen/steve-active-benchmark/2026-10-09')
s=json.loads((root/'summary.json').read_text())
for sweep in (12,40,77):
 started=time.monotonic()
 code="import sys;sys.path.insert(0,'scripts');import oa203_return_scanner as s;from pathlib import Path;from datetime import date;a=s.DayArchive(Path('/home/owen/steve-active-benchmark'),date(2026,10,9),'oa203/scanner',False);print(s.build_active_snapshot(a,s.Config(upload=False),"+str(sweep)+",1791552600000,1791576000000))"
 cmd=['/usr/bin/time','-v',sys.executable,'-c',code]
 r=subprocess.run(cmd,env={**os.environ,'OA203_UPLOAD':'0'},capture_output=True,text=True,timeout=240,preexec_fn=constrain_builder)
 report={'sweep':sweep,'seconds':time.monotonic()-started,'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
 Path(f'/tmp/steve-active-benchmark-{sweep}.json').write_text(json.dumps(report,indent=2))
 print(json.dumps(report),flush=True)
 if r.returncode:break
