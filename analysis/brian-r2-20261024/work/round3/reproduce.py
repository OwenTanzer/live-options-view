from pathlib import Path
import shutil,subprocess,sys,json,hashlib
R=Path(__file__).resolve().parents[2];T=R/'work/private/round3-reproduction';mode=sys.argv[1] if len(sys.argv)>1 else 'run'
if mode=='run':
 assert not T.exists(),'Preserve existing replay; do not overwrite it'
 for sub in ['work/round3','work/private','outputs/round2']:(T/sub).mkdir(parents=True,exist_ok=True)
 for f in (R/'work/round3').glob('*.py'):shutil.copy2(f,T/'work/round3'/f.name)
 for name in ['protocol.json','download-manifest.json']:shutil.copy2(R/'outputs'/name,T/'outputs'/name)
 for name in ['protocol.json','round3-plan.json']:shutil.copy2(R/'outputs/round2'/name,T/'outputs/round2'/name)
 for f in (R/'work/private').glob('history__QQQ__5__20*.json'):shutil.copy2(f,T/'work/private'/f.name)
 for script in ['preflight.py','run_final.py','score.py']:
  p=subprocess.run([sys.executable,str(T/'work/round3'/script)],capture_output=True,text=True);(T/(script+'.log')).write_text(p.stdout+p.stderr);assert p.returncode==0,(script,p.stderr)
 print('REPLAY_COMPLETE; run compare after original scoring')
elif mode=='compare':
 checks=[]
 for f in sorted((T/'outputs/round3').glob('*.csv')):
  norm=lambda p:p.read_bytes().replace(b'\r\n',b'\n');original=R/'outputs/round3'/f.name;same=norm(original)==norm(f);checks.append({'file':f.name,'exact_LF_bytes':same,'sha256':hashlib.sha256(norm(f)).hexdigest()});assert same,f.name
 assert len(checks)>=13
 out={'all_passed':True,'checks':checks,'method':'Fresh private directory, copied frozen protocols and byte-identical source objects; rerun preflight/run_final/score. Exact LF CSV comparisons; run timestamps intentionally differ. Final remains consumed.','replay_preflight':json.loads((T/'outputs/round3/preflight.json').read_text()),'replay_consumption':json.loads((T/'outputs/round3/consumption.json').read_text())};(R/'outputs/round3/reproduction.json').write_text(json.dumps(out,indent=2));print(json.dumps({'all_passed':True,'tables':len(checks)},indent=2))
else:raise ValueError(mode)
