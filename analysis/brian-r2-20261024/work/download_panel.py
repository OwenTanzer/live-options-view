import json,requests,hashlib,datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[1]; D=ROOT/'work'/'private';D.mkdir(exist_ok=True)
BASE='https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/'
keys=[f'history/NVDA/5/{y}.json' for y in [2024,2025,2026]]+[f'history/QQQ/5/{y}.json' for y in [2023,2024,2025,2026]]+['history/QQQ/options_0dte/manifest.json']
def read(k):
    try:
        r=requests.get(BASE+k,timeout=30);r.raise_for_status();raw=r.content; (D/k.replace('/','__')).write_bytes(raw)
        return {'key':k,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'retrieved':datetime.datetime.now(datetime.timezone.utc).isoformat(),'last_modified':r.headers.get('Last-Modified'),'etag':r.headers.get('ETag')}
    except Exception as e:return {'key':k,'error':str(e)}
r=list(ThreadPoolExecutor(4).map(read,keys));(ROOT/'outputs'/'download-manifest.json').write_text(json.dumps(r,indent=2)); print(json.dumps(r,indent=2))
