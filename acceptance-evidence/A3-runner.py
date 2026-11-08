from pathlib import Path
import json,hashlib,requests,subprocess,sys,ctypes,datetime,os
root=Path(__file__).parent
r=root/'analysis/brian-r2-20261024'
e=root/'acceptance-evidence';e.mkdir(exist_ok=True)
b=ctypes.create_string_buffer(12);assert ctypes.windll.kernel32.GetSystemPowerStatus(b) and b.raw[0]==1,'AC required'
manifest=json.loads((r/'outputs/download-manifest.json').read_text())
d=r/'work/private';d.mkdir(exist_ok=True)
checks=[]
for rec in manifest:
 if rec['key'].startswith('history/QQQ/5/'):
  q=requests.get('https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/'+rec['key'],timeout=60);q.raise_for_status()
  assert hashlib.sha256(q.content).hexdigest()==rec['sha256'],rec['key']
  (d/rec['key'].replace('/','__')).write_bytes(q.content);checks.append({'key':rec['key'],'sha256':rec['sha256'],'bytes':len(q.content)})
record={'start':datetime.datetime.now(datetime.timezone.utc).isoformat(),'revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),'ACLineStatus':b.raw[0],'sources':checks,'python':sys.version}
(e/'replay-start.json').write_text(json.dumps(record,indent=2));print('Sources verified; full replay starting',flush=True)
for mode in ['run','compare']:
 with (e/('replay-'+mode+'.log')).open('w') as log:
  p=subprocess.run([sys.executable,'work/round3/reproduce.py',mode],cwd=r,stdout=log,stderr=subprocess.STDOUT)
 record[mode+'_exit']=p.returncode
 (e/'replay-status.json').write_text(json.dumps(record,indent=2))
 assert p.returncode==0,mode
record['end']=datetime.datetime.now(datetime.timezone.utc).isoformat();(e/'replay-status.json').write_text(json.dumps(record,indent=2));print('REPLAY AND COMPARE PASSED',flush=True)
