from pathlib import Path
import json,hashlib,subprocess,datetime
R=Path(__file__).resolve().parents[2];files=[R/'research/synthetic_days/README.md',R/'research/synthetic_days/r2_sources.py',Path('C:/Users/Owen/Documents/moopertonic_projects/live-options-view/qqq-data-pipeline/backfill_intraday.py')];out={'retrieved_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'files':[{'path':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in files],'checks':[]}
commands=[['git','-C',str(R/'research'),'log','--all','--name-only','--format=','--',':(glob)**/backfill_history.py','backfill_history.py'],['git','-C',str(R/'research'),'rev-parse','--is-shallow-repository']]
for c in commands:
 p=subprocess.run(c,capture_output=True,text=True);out['checks'].append({'command':c,'exit':p.returncode,'output':p.stdout.strip(),'stderr':p.stderr.strip()})
out['other_read_only_searches']={'local':'rg --files C:/Users/Owen/Documents -g *backfill_history*; no matches','github':'gh search code filename:backfill_history.py --owner OwenTanzer --json path,repository,url; returned []','ceiling':'Search absence does not establish nonexistence in other repositories, deleted/unfetched histories or another host.'}
(R/'outputs/round4/provenance-evidence.json').write_text(json.dumps(out,indent=2))
