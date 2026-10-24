from pathlib import Path
import subprocess,tarfile,io,json,hashlib,shutil,sys,datetime
R=Path(__file__).resolve().parents[2];O=R/'outputs/round4';O.mkdir(parents=True,exist_ok=True);T=R/'work/private/round4-published';commit='88c7fbd7ae12002c2013ada815a93e56ef1190e0';prefix='analysis/brian-r2-20261024'
assert not T.exists(),'Preserve prior published-package replay'
repo=R/'research' if (R/'research').is_dir() else Path(subprocess.check_output(['git','rev-parse','--show-toplevel'],cwd=R,text=True).strip())
archive=subprocess.check_output(['git','archive','--format=tar',commit,prefix],cwd=repo);T.mkdir()
with tarfile.open(fileobj=io.BytesIO(archive)) as tar:tar.extractall(T,filter='data')
P=T/prefix;manifest=json.loads((P/'artifact-manifest.json').read_text());sha=lambda b:hashlib.sha256(b).hexdigest()
assert all(sha((P/r['path']).read_bytes())==r['sha256'] for r in manifest)
(P/'work/private').mkdir(exist_ok=True)
for f in (R/'work/private').glob('history__QQQ__5__20*.json'):shutil.copy2(f,P/'work/private'/f.name)
record={'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'published_commit':commit,'archive_sha256':sha(archive),'artifact_hashes_verified':len(manifest),'final_status':'consumed; reproduction only, no retuning','commands':[]};(O/'published-replay-start.json').write_text(json.dumps(record,indent=2))
for mode in ['run','compare']:
 command=[sys.executable,str(P/'work/round3/reproduce.py'),mode];p=subprocess.run(command,capture_output=True,text=True);(T/('replay-'+mode+'.log')).write_text(p.stdout+p.stderr);record['commands'].append({'command':'python work/round3/reproduce.py '+mode,'exit':p.returncode});assert p.returncode==0,p.stderr
rep=json.loads((P/'outputs/round3/reproduction.json').read_text());record.update(completed_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),all_passed=rep['all_passed'],checks=rep['checks'],replay_consumption=rep['replay_consumption']);(O/'published-reproduction.json').write_text(json.dumps(record,indent=2));print(json.dumps({'all_passed':record['all_passed'],'tables':len(rep['checks']),'commit':commit},indent=2))
