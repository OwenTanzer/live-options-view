from pathlib import Path
import json,hashlib,datetime,platform,sys
R=Path(__file__).resolve().parents[2];O=R/'outputs/round3';O.mkdir(parents=True,exist_ok=True)
assert not (O/'preflight.json').exists(),'Existing preflight must not be silently replaced'
sha=lambda b:hashlib.sha256(b).hexdigest();files={}
for name in ['outputs/protocol.json','outputs/round2/protocol.json','outputs/round2/round3-plan.json']:
 b=(R/name).read_bytes();files[name]={'sha256_exact':sha(b),'sha256_LF':sha(b.replace(b'\r\n',b'\n'))}
manifest=json.loads((R/'outputs/download-manifest.json').read_text());sources=[]
for rec in manifest:
 if rec['key'].startswith('history/QQQ/5/'):
  b=(R/'work/private'/rec['key'].replace('/','__')).read_bytes();assert sha(b)==rec['sha256'];sources.append({'key':rec['key'],'bytes':len(b),'sha256':sha(b),'matches_increment1':True})
assert len(sources)==4
spec={'run_registered_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'registered before constructing/scoring final outcomes','protocols':files,'sources':sources,'source_bucket':'qqq-options-chain-data','code':{f.name:sha(f.read_bytes().replace(b'\r\n',b'\n')) for f in sorted((R/'work/round3').glob('*.py'))},'python':sys.version,'platform':platform.platform(),'seed':20261024,'models':'state20/state60/fixed equal; unconditional20/unconditional60/fixed equal; no tuning','timing':'Primary assumes start-labeled5min bars, close at t+300 and availabilityt+360. Delayed deployment availablet+660 and shifts/refits target; not same-target staleness isolation.'}
(O/'preflight.json').write_text(json.dumps(spec,indent=2));print(json.dumps(spec,indent=2))
