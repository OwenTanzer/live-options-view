from pathlib import Path
import shutil,subprocess,sys,json,hashlib,platform
R=Path(__file__).resolve().parents[2];T=R/'work/private/round2-reproduction';(T/'work/round2').mkdir(parents=True,exist_ok=True);(T/'work/private').mkdir(exist_ok=True);(T/'outputs/round2').mkdir(parents=True,exist_ok=True)
for f in (R/'work/round2').glob('*.py'):shutil.copy2(f,T/'work/round2'/f.name)
for name in ['protocol.json']:shutil.copy2(R/'outputs'/name,T/'outputs'/name)
shutil.copy2(R/'outputs/round2/protocol.json',T/'outputs/round2/protocol.json')
for f in (R/'work/private').glob('history__*__5__20*.json'):shutil.copy2(f,T/'work/private'/f.name)
for name in ['validation-predictions.csv','cross-predictions.csv']:shutil.copy2(R/'work/private'/name,T/'work/private'/name)
logs=[]
for script in ['factorial.py','nvda_check.py','positive_link.py']:
 p=subprocess.run([sys.executable,str(T/'work/round2'/script)],capture_output=True,text=True);(T/(script+'.log')).write_text(p.stdout+p.stderr);assert p.returncode==0,(script,p.stderr);logs.append({'command':'python work/round2/'+script,'exit':p.returncode})
checks=[]
for f in sorted((T/'outputs/round2').glob('*.csv')):
 original=R/'outputs/round2'/f.name;norm=lambda p:p.read_bytes().replace(b'\r\n',b'\n');same=norm(original)==norm(f);checks.append({'file':f.name,'exact_normalized_bytes':same,'sha256':hashlib.sha256(norm(f)).hexdigest()});assert same,f.name
out={'commands':logs,'checks':checks,'all_passed':True,'final_scored':False,'python':sys.version,'platform':platform.platform(),'note':'Fresh directory replay with same frozen protocol and byte-preserved inputs. Freeze scripts deliberately not rerun; positive protocol timestamp regenerated only in private replay. Per-origin predictions and source bytes stay private.'};(R/'outputs/round2/reproduction.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
