from pathlib import Path
import shutil,json,hashlib
R=Path(__file__).resolve().parents[2];T=R/'research/analysis/brian-r2-20261024'
replay=json.loads((R/'outputs/round4/published-reproduction.json').read_text());assert replay['all_passed'] and len(replay['checks'])==13
for sub in ['work/round4','outputs/round4']:
 (T/sub).mkdir(parents=True,exist_ok=True)
 for f in (R/sub).iterdir():
  if f.is_file() and f.suffix in ['.py','.md','.json','.csv']:(T/sub/f.name).write_bytes(f.read_bytes().replace(b'\r\n',b'\n'))
readme=T/'README.md';s=readme.read_text();s=s[s.index('\n\n')+2:] if s.startswith('> Latest checkpoint:') else s
readme.write_bytes(('> Latest checkpoint: [final audit and handoff](outputs/round4/handoff.md). Four increments completed; final remains consumed. Earlier statements below describe historical checkpoints. Draft PR remains unmerged.\n\n'+s).encode())
files=[f for f in sorted(T.rglob('*')) if f.is_file() and f.name!='artifact-manifest.json' and not {'private','__pycache__'}&set(f.parts)]
manifest=[{'path':str(f.relative_to(T)).replace('\\','/'),'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in files];(T/'artifact-manifest.json').write_bytes(json.dumps(manifest,indent=2).encode())
cons=json.loads((T/'outputs/round3/consumption.json').read_text());assert hashlib.sha256((T/'outputs/round3/preflight.json').read_bytes()).hexdigest()==cons['preflight_sha256'];print('PACKAGED',len(manifest),'original_preflight_chain_verified')
