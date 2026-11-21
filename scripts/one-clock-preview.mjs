// Read-only, disposable browser fixture on a separate loopback port. Reuses
// the normal preview gateway/CSP, but never offers account mutations here.
import http from 'node:http';
import { createHash } from 'node:crypto';
import { createPreview } from './preview-local.mjs';
import { handleOneClockChain } from '../backend/one-clock-chain.js';
import { readBotLedgerPage } from '../backend/one-clock-bots.js';
import { handleOneClockSqueeze } from '../backend/one-clock-squeeze.js';
import { sessionInstant } from '../site/shared/one-clock-session.js';
const preview=createPreview();
const day='2026-11-09',T=sessionInstant(day,9,30),objects=new Map();
for(const symbol of ['QQQ','SPY','IBIT']) for(const minute of [0,5]) {
  const prefix=symbol==='QQQ'?'intraday/':`intraday/${symbol.toLowerCase()}/`;
  const key=prefix+'20261109/snapshot_093'+minute+'00000000.csv';
  const at=T+minute*60000,spot=symbol==='IBIT'?40:660;
  const rows=['Strike,Type,OpenInterest,Expiration,UnderlyingPrice,Bid,Ask'];
  for(const strike of [spot-2,spot-1,spot,spot+1,spot+2]) for(const type of ['call','put']) rows.push(`${strike},${type},${type==='call'?100:50},${symbol==='IBIT'?'2026-11-13':day},${spot},1,2`);
  objects.set(key,{text:rows.join('\n'),uploaded:new Date(at+500),etag:'csv-'+symbol+minute});
  const meta={schema_version:'snapshot_provenance.v1',snapshot_key:key,symbol,constructed_at:new Date(at).toISOString(),
    latest_published_at:new Date(at+1000).toISOString(),publication:{status:'published'},market_readings:[{
      schema_version:'market_reading.v1',reading_id:'reference_oi_skew',symbol,observed_at:new Date(at).toISOString(),
      input:{snapshot_key:key,snapshot_key_status:'valid',snapshot_timestamp:new Date(at).toISOString()},
      published_at:new Date(at+1000).toISOString(),publication:{status:'published'},interpretation:{kind:'reference'},
      status:{code:'ok'},freshness:{status:'live'},values:{imbalance_ratio:1/3,near_money_call_oi:500,near_money_put_oi:250,band_strikes:5},
      calculation:{version:'reference_oi_skew.v1',parameters:{near_money_pct:0.02}}}]};
  objects.set(key.replace('.csv','.provenance.json'),{text:JSON.stringify(meta),uploaded:new Date(at+1500),etag:'meta-'+symbol+minute});
}
const bucket={async list({prefix}){return {objects:[...objects.keys()].filter(key=>key.startsWith(prefix)).map(key=>({key,uploaded:objects.get(key).uploaded})),truncated:false};},
  async get(key){const row=objects.get(key);return row?{...row,size:Buffer.byteLength(row.text),text:async()=>row.text}:null;}};
const bot='synthetic_bot',trades=[];
for(let i=0;i<105;i++) {
  const id=`00000000-0000-4000-8000-${String(i).padStart(12,'0')}`,at=i===0?T-86400000:T+i*1000;
  const trade={username:bot,status:'filled',execution_request_id:id,ts:new Date(at).toISOString(),
    sym:'QQQ261113C00660000',underlying:'QQQ',instrument_type:'option',side:i===104?'sell':'buy',qty:i===0?10:i===104?4:1,
    price:2,strike:660,type:'call',exp:'2026-11-13',multiplier:100};
  trades.push(trade);objects.set(`paper-trades/requests/${id}.json`,{text:JSON.stringify(trade),uploaded:new Date(at+500),etag:String(i)});
}
const hash=text=>createHash('sha256').update(text).digest('hex');
const squeezeRun='11111111-1111-4111-8111-111111111111',squeezeBase='squeeze-scanner/v1/scheduled/runs/'+squeezeRun;
const squeezeResults=JSON.stringify([{ticker:'ABC',comparison_eligible:true,ranks:{combined:1},scores:{combined:.7,factor:.8,options:.6}}]);
const squeezeManifest=JSON.stringify({schema_version:1,run_id:squeezeRun,started_at:new Date(T-60000).toISOString(),finished_at:new Date(T).toISOString(),status:'complete',scoring:{version:'synthetic-fixture'},
  files:{'results.json':{sha256:hash(squeezeResults),bytes:Buffer.byteLength(squeezeResults)}}});
for(const [key,text] of [[squeezeBase+'/results.json',squeezeResults],[squeezeBase+'/manifest.json',squeezeManifest],
  [squeezeBase+'/COMMITTED.json',JSON.stringify({run_id:squeezeRun,manifest_sha256:hash(squeezeManifest)})],
  [`squeeze-scanner/v1/scheduled/publication-attempts/${squeezeRun}/22222222-2222-4222-8222-222222222222/finished.json`,JSON.stringify({run_id:squeezeRun,archive:squeezeBase,archive_verified:true,latest:'advanced',at:new Date(T+1000).toISOString()})]]) {
  objects.set(key,{text,uploaded:new Date(T+1500),etag:hash(text)});
}
preview.env.API={fetch:request=>{
  const url=new URL(request.url);
  if(url.pathname==='/api/one-clock/chain')return handleOneClockChain(request,{PAPER_TRADES:bucket},url);
  if(url.pathname==='/api/one-clock/squeeze')return handleOneClockSqueeze(request,{PAPER_TRADES:bucket},url);
  if(url.pathname==='/api/one-clock/bots/roster')return Response.json({bots:[bot],membershipBasis:'synthetic-fixture'});
  if(url.pathname==='/api/one-clock/bots')return readBotLedgerPage(bucket,{trades},{bot,session:day,instrument:url.searchParams.get('instrument'),
    cutoff:Number(url.searchParams.get('cutoff')),offset:Number(url.searchParams.get('offset')),version:url.searchParams.get('version')}).then(body=>Response.json(body,{status:body.restart?409:200}));
  return new Response('{}',{status:404,headers:{'Content-Type':'application/json'}});
}};
const server=http.createServer(async(req,res)=>{
  try {
    if(req.headers.host!==`localhost:${server.address().port}` || req.method!=='GET'){res.writeHead(403);res.end();return;}
    const response=await preview.fetch(new Request('http://localhost:8787'+req.url));
    const headers=Object.fromEntries(response.headers);
    res.writeHead(response.status,headers);res.end(Buffer.from(await response.arrayBuffer()));
  }catch(error){res.writeHead(500);res.end(String(error.message));}
});
server.listen(0,'127.0.0.1',()=>console.log(`Read-only synthetic One Clock preview http://localhost:${server.address().port} · Nov 9 at09:30:01.500 or09:35:01.500 New York`));
for(const signal of ['SIGINT','SIGTERM'])process.once(signal,()=>server.close(()=>{preview.close();process.exit(0);}));
