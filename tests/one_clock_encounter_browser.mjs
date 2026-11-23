// Browser drives the real account handler and migrated SQLite D1 shim. Only
// the signed-in identity and upstream immutable market publications are fixtures.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';
import {makeStorage} from './helpers/d1_shim.mjs';
import {createStorage} from '../backend/storage.js';
import {handleOneClockResults} from '../backend/one-clock-results.js';
import {levelIngestion} from '../backend/one-clock-level-ingestion.js';
import {advanceOneClockResearch,handleOneClockResearch} from '../backend/one-clock-research.js';
const DB=makeStorage(), session='2026-11-20', stream='collector-chain/QQQ', start=Date.parse(session+'T15:00:00Z');
const origin='https://options.moopertonic.net', objects=new Map();
const PAPER_TRADES={async get(key){return objects.get(key)??null;}};
const env={DB,PAPER_TRADES,ONE_CLOCK_OBSERVER:'enabled',ONE_CLOCK_PUBLICATIONS:'enabled'};
for(const file of ['0004_one_clock.sql','0010_one_clock_ingestion.sql','0011_one_clock_publications.sql','0012_one_clock_handoff.sql','0013_one_clock_reevaluation.sql','0014_one_clock_chain_evidence.sql']) {
  await DB.exec(await readFile(new URL('../backend/migrations/'+file,import.meta.url),'utf8'));
}
function object(key,text,uploaded) {
  const bytes=Buffer.from(text), sha256=createHash('sha256').update(bytes).digest('hex');
  objects.set(key,{size:bytes.length,etag:sha256,uploaded:new Date(uploaded),text:async()=>text,arrayBuffer:async()=>Uint8Array.from(bytes).buffer});
  return sha256;
}
const ranges='Tier,StrikeOffset,Call_p25,Call_p50,Call_p75,Call_p90,Put_p25,Put_p50,Put_p75,Put_p90,Call_adj,Put_adj\n'+
  [-2,-1,0,1,2].map(n=>'0DTE_Regular,'+n+',10,20,30,40,10,20,30,40,1,1').join('\n');
object('derived/OIranges.csv',ranges,start-86400000);
const sample=[[0,698],[1,699],[2,700],[3,701],[5,699],[11,700],[29,700]];
for(const [i,[minute,spot]] of sample.entries()) {
  const at=start+minute*60000, sequence=i+1, csvKey='intraday/20261120/snapshot_10'+String(minute).padStart(2,'0')+'00000000.csv';
  const id='native:'+stream+':'+csvKey, evidenceKey='test-entry-'+sequence;
  const csv='Strike,Type,OpenInterest,Expiration,UnderlyingPrice\n'+[699,700,701].flatMap((strike,j)=>['call','put'].map(side=>{
    const oi=(j===0&&side==='put'||j===1&&side==='call'||j===2)?50:10;
    return [strike,side,oi,session,spot].join(',');
  })).join('\n');
  const json=JSON.stringify({symbol:'QQQ',date:session,expiration:session,tier:'0DTE_Regular',timestamp:new Date(at).toISOString(),snapshot_key:csvKey,underlying_market:{freshness:'live',spot_ts:new Date(at).toISOString()},underlying_price:spot});
  const evidence=[[csv,'collector.snapshot.csv.v1','text/csv'],[json,'collector.snapshot.payload.v1','application/json']].map(([text,schema,contentType])=>{
    const sha256=createHash('sha256').update(text).digest('hex'),key='publications/v1/'+stream+'/blobs/'+sha256;
    object(key,text,at);return {key,sha256,bytes:Buffer.byteLength(text),schema,contentType};
  });
  const price={source:'price',id,revision:1,session,instrument:'QQQ',origin:'recorded',eventAt:at,availableAt:at,evidence:evidenceKey,value:{spot},details:{originalRecordId:csvKey,sequence,evidence}};
  await DB.prepare('INSERT INTO one_clock_publication_entries VALUES(?,?,?,?,?,?,?)').bind(stream,sequence,evidenceKey,'digest',at,at,JSON.stringify({records:[price]})).run();
  await DB.prepare('INSERT INTO one_clock_publication_records VALUES(?,?,?,?,?,?,?,?,?,?,?)').bind(stream,'price',id,1,session,'QQQ',at,at,at,sequence,JSON.stringify(price)).run();
}
await DB.prepare('INSERT INTO one_clock_publication_status VALUES(?,?,?,?)').bind(stream,1,start+30*60000,JSON.stringify({nextSequence:sample.length+1,reason:'tail-at-check'})).run();
const realNow=Date.now;
let simulatedNow=start-60000;
Date.now=()=>simulatedNow;
const storage=createStorage(env);
await storage.users.insert('tester',{username:'tester',version:0});await storage.sessions.create('local-token','tester',3600);
const browser=await chromium.launch({headless:true,channel:'chromium',executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
try {
  const page=await browser.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.clock.setFixedTime(new Date(start+31*60000));
  await page.context().addCookies([{name:'session',value:'local-token',url:origin}]);
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());assert.equal(url.origin,origin);
    if(['/api/one-clock/research','/api/one-clock/results'].includes(url.pathname)){
      const handler=url.pathname.endsWith('/results')?handleOneClockResults:handleOneClockResearch;
      const res=await handler(new Request(url,{method:request.method(),headers:await request.allHeaders(),body:request.postData()??undefined}),env);
      const body=await res.text();console.log(request.postDataJSON()?.action??'load',res.status,body.slice(0,130));
      return route.fulfill({status:res.status,body,contentType:'application/json'});
    }
    assert.match(url.pathname,/^\/(one-clock\.(html|css)|(?:js|shared)\/[a-z0-9-]+\.js)$/);
    return route.fulfill({contentType:url.pathname.endsWith('.js')?'text/javascript':url.pathname.endsWith('.css')?'text/css':'text/html',body:await readFile(new URL('../site'+url.pathname,import.meta.url))});
  });
  await page.goto(origin+'/one-clock.html');
  await page.click('#new-rule');await page.selectOption('#study-kind','level');await page.fill('#rule-name','QQQ bright OI');
  await page.click('#account-save');await page.waitForFunction(()=>document.querySelector('#form-status').textContent.includes('to your account'));
  await page.click('#account-activate');await page.waitForFunction(()=>document.querySelector('#form-status').textContent.includes('activated'));
  simulatedNow=start+30*60000;
  // Native producer evidence is already in D1; run the real scheduled
  // observer with the study's registered adapter and its ordinary work queue.
  const ticks=await advanceOneClockResearch(env,{now:simulatedNow,sources:[levelIngestion],publicationSources:[]});
  assert.ok(ticks.some(t=>t.state==='evaluated' && t.session===session),JSON.stringify(ticks));
  simulatedNow+=60000;
  await page.click('#show-observed');await page.waitForSelector('#observed-ledger button');
  await page.locator('#observed-ledger button').first().click();assert.match(await page.locator('#observed-evidence pre').textContent(),/observerRevision/);
  const observed=await page.locator('#observed-ledger tr').count();
  await page.click('#show-builder');
  await page.fill('#encounter-day',session);await page.click('#encounter-run');
  await page.waitForSelector('#observed-results:not([hidden]) table caption');
  const table=page.locator('#observed-results table').filter({has:page.locator('caption')});
  assert.equal(await table.locator('tbody tr').count(),12);
  assert.equal(await page.locator('#observed-ledger tr').count(),observed);
  for(const group of ['put-only','call-only','both'])assert.ok(Number(await table.locator('tbody tr').filter({has:page.getByText(group,{exact:true})}).first().locator('td').nth(2).innerText())>0);
  await page.locator('#observed-ledger button').first().click();assert.match(await page.locator('#observed-evidence').innerText(),/calibration/);
  const historical=new URL(await page.getByRole('link',{name:'Open Historical at the encounter'}).getAttribute('href'),origin);
  assert.equal(historical.hash,'#history-clock');assert.equal(historical.searchParams.get('one_clock_session'),session);
  assert.equal(Number(historical.searchParams.get('one_clock_at')),start+60000);
  await page.click('#show-builder');await page.selectOption('#level-bucket','4');await page.selectOption('#level-approach','down');
  await page.fill('#level-horizons','2, 5');await page.uncheck('#both');await page.click('#account-save');
  await page.waitForFunction(()=>document.querySelector('#form-status').textContent.includes('v2 to your account'));
  await page.click('#encounter-run');await page.waitForSelector('#observed-results:not([hidden]) caption');assert.equal(await table.locator('tbody tr').count(),4);
  assert.deepEqual(errors,[]);console.log('Passed browser account save, observation, archived replay, three exclusive groups, four horizons, receipt inspection and second version.');
} finally {Date.now=realNow;await browser.close();DB.db.close();}
