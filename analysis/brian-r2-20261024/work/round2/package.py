from pathlib import Path
import shutil,json,hashlib
R=Path(__file__).resolve().parents[2];T=R/'research/analysis/brian-r2-20261024'
for sub in ['work/round2','outputs/round2']:(T/sub).mkdir(parents=True,exist_ok=True)
for f in (R/'work/round2').glob('*.py'):shutil.copy2(f,T/'work/round2'/f.name)
for f in (R/'outputs/round2').iterdir():
 if f.is_file():shutil.copy2(f,T/'outputs/round2'/f.name)
readme=T/'README.md';s=readme.read_text();link='\n\nIncrement 2: [factorial conditioning/memory audit, failed tests and frozen round-3 plan](outputs/round2/checkpoint.md). Final remains unscored. See outputs/round2/reproduction.json for 13 exactly reproduced tables.\n'
if 'Increment 2: [factorial' not in s:readme.write_text(s+link)
(T/'.gitignore').write_text('work/private/\n**/__pycache__/\nwork/round2/*.txt\nwork/round2/*.b64\n')
for f in T.rglob('*'):
 if f.is_file() and f.suffix!='.png' and 'private' not in f.parts:f.write_bytes(f.read_bytes().replace(b'\r\n',b'\n'))
manifest=[{'path':str(f.relative_to(T)).replace('\\','/'),'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in sorted(T.rglob('*')) if f.is_file() and f.name!='artifact-manifest.json' and 'private' not in f.parts]
(T/'artifact-manifest.json').write_bytes(json.dumps(manifest,indent=2).encode());print('PACKAGED',len(manifest))
