from pathlib import Path
import shutil,json,hashlib
R=Path(__file__).resolve().parents[2];T=R/'research/analysis/brian-r2-20261024'
(T/'.gitattributes').write_bytes(b'* -text\n# These provenance records retain exact original Windows bytes for their hash chain.\noutputs/round3/preflight.json whitespace=cr-at-eol\noutputs/round3/consumption.json whitespace=cr-at-eol\n')
for sub in ['work/round3','outputs/round3']:(T/sub).mkdir(parents=True,exist_ok=True)
for f in (R/'work/round3').glob('*.py'):shutil.copy2(f,T/'work/round3'/f.name)
for f in (R/'outputs/round3').iterdir():
 if f.is_file():shutil.copy2(f,T/'outputs/round3'/f.name)
readme=T/'README.md';s=readme.read_text();notice='> Latest checkpoint: [increment 3 final-period results](outputs/round3/checkpoint.md). The final segment is now consumed. Earlier unscored statements below describe their historical checkpoints.\n\n'
if not s.startswith('> Latest checkpoint:'):readme.write_text(notice+s)
# Preserve exact preflight and consumption bytes: consumption references exact preflight SHA.
for f in (T/'work/round3').glob('*.py'):f.write_bytes(f.read_bytes().replace(b'\r\n',b'\n'))
for f in (T/'outputs/round3').iterdir():
 if f.suffix!='.png' and f.name not in ['preflight.json','consumption.json']:f.write_bytes(f.read_bytes().replace(b'\r\n',b'\n'))
readme.write_bytes(readme.read_bytes().replace(b'\r\n',b'\n'))
files=[f for f in sorted(T.rglob('*')) if f.is_file() and f.name!='artifact-manifest.json' and not {'private','__pycache__'}&set(f.parts)]
manifest=[{'path':str(f.relative_to(T)).replace('\\','/'),'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in files];(T/'artifact-manifest.json').write_bytes(json.dumps(manifest,indent=2).encode())
cons=json.loads((T/'outputs/round3/consumption.json').read_text());assert hashlib.sha256((T/'outputs/round3/preflight.json').read_bytes()).hexdigest()==cons['preflight_sha256'];print('PACKAGED',len(manifest),'preflight_chain_verified')
