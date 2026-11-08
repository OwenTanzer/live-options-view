import json,urllib.request,hashlib
base='https://ghcr.io';pkg='owentanzer/market-mcp-read-only';digest='sha256:a649beb7a23c0f2698f8a7bb89edf58d07f5c1cb4bc7692896dfebdd3cb3c851'
t=json.load(urllib.request.urlopen(base+'/token?scope=repository:'+pkg+':pull&service=ghcr.io'))['token']
rows=[]
for ref in ['62ae9ffc36b2af8707e6709ca816bb1baf9d81df-run-202611081850-1',digest]:
 q=urllib.request.Request(base+'/v2/'+pkg+'/manifests/'+ref,headers={'Authorization':'Bearer '+t,'Accept':'application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'})
 with urllib.request.urlopen(q) as r:
  b=r.read();actual='sha256:'+hashlib.sha256(b).hexdigest();assert actual==digest
  rows.append({'ref':ref,'digest':actual,'status':r.status,'config':json.loads(b)['config']['digest']})
print(json.dumps(rows,indent=2));open('acceptance-evidence/registry-verified.json','w').write(json.dumps(rows,indent=2))
