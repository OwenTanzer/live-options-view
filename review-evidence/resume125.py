import json
from concurrent.futures import ProcessPoolExecutor
from replay125 import run,DAYS,OUT
if __name__=='__main__':
 pending=[d for d in DAYS if not (OUT/(d+'.json')).exists()]
 with ProcessPoolExecutor(max_workers=2) as ex:
  list(ex.map(run,pending))
 results=[{k:v for k,v in json.loads((OUT/(d+'.json')).read_text()).items() if k not in ('sources','controls','reasons','top10')} for d in DAYS]
 (OUT/'summary125.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
 print('ALL COMPLETE',flush=True)
