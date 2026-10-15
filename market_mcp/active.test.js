'use strict';
const test=require('node:test');const assert=require('node:assert/strict');
const {Consumer}=require('./consumer');const {Source,DataError,hash,LIMITS,permitted}=require('./source');
const {Protocol,tools}=require('./server');
const {gzipSync,gunzipSync}=require('node:zlib');
const {fixtures,activeFixtures,SESSION,NOW,CONTRACT}=require('./fixtures');
const BASE=`oa203/scanner/${SESSION}/`, ACTIVE=BASE+'inprogress/';
function setup(){
  const {files,prefix,pointer,summary,manifest}=activeFixtures({withoutFinal:true}), calls=[];
  const source=new Source({read:async key=>{calls.push(key);if(!Object.hasOwn(files,key))throw new DataError('missing_artifact','absent',key);return files[key];}});
  const consumer=new Consumer(source,{now:()=>NOW});
  return {files,calls,consumer,prefix,pointer,summary,manifest};
}
test('active all, clean, qualified and pinned detail survive pointer advance',async()=>{
  const f=setup();
  const all=await f.consumer.call('active_rankings',{session:SESSION,view:'all',limit:1});
  assert.equal(all.status,'partial');assert.equal(all.actual.sweep,6);assert.equal(all.rows[0].symbol,CONTRACT);
  assert.equal(all.freshness.status,'stale');
  const clean=await f.consumer.call('active_rankings',{session:SESSION,view:'clean'});
  assert.equal(clean.rows.length,1);assert.equal(clean.rows[0].clean_rank,'1');
  const qualified=await f.consumer.call('active_rankings',{session:SESSION,view:'qualified_ask_bid_v1'});
  assert.equal(qualified.rows[0].qualified_first_ask_to_later_bid_rank,'1');
  f.files[ACTIVE+'latest.json']='{}';
  const detail=await f.consumer.call('result_detail',{reference:all.rows[0].detail_reference,limit:6});
  assert.equal(detail.status,'in_progress');assert.equal(detail.actual.sweep,6);
  assert.equal(detail.quote_path.length,2);assert.ok(detail.quote_path.every(r=>r.sweep<=6));
  assert.equal(detail.pagination.next_offset,2);
  const last=await f.consumer.call('result_detail',{reference:all.rows[0].detail_reference,limit:6,offset:4});
  assert.equal(last.quote_path.length,2);assert.equal(last.quote_path.at(-1).sweep,6);
  assert.equal(last.pagination.next_offset,null);
  assert.ok(!f.calls.some(k=>k.includes('sweep_0007')));
});
test('active cadence reports late refresh from elapsed time with producer degradation',async()=>{
  const f=setup();const late=Date.parse(f.pointer.published_at);
  f.consumer.now=()=>late+60000;
  Object.assign(f.pointer,{observed_cadence_s:3459.3,cadence:{basis:'elapsed_time',sweeps:6,max_sweep_s:1001,
    budget_exhausted_sweeps:2,server_errors:83,transport_errors:0}});
  f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
  const out=await f.consumer.call('active_rankings',{session:SESSION});
  assert.equal(out.cadence.status,'late');assert.equal(out.cadence.basis,'elapsed_time');
  assert.equal(out.cadence.degradation.max_sweep_seconds,1001);assert.equal(out.cadence.degradation.budget_exhausted_sweeps,2);
  assert.ok(out.warnings.some(w=>w.includes('3459.3s')));
});
test('active cadence without producer report still judges elapsed time, and flags overdue pointers',async()=>{
  const f=setup();const published=Date.parse(f.pointer.published_at);
  delete f.pointer.cadence;f.pointer.observed_cadence_s=1836;f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
  f.consumer.now=()=>published+60000;
  const onTime=await f.consumer.call('active_rankings',{session:SESSION});
  assert.equal(onTime.cadence.status,'on_time');assert.equal(onTime.cadence.basis,'publication_interval');
  assert.equal(onTime.cadence.degradation,null);
  f.consumer.now=()=>published+2701000;
  const overdue=await f.consumer.call('active_rankings',{session:SESSION});
  assert.equal(overdue.cadence.status,'overdue');assert.equal(overdue.cadence.seconds_since_publication,2701);
  assert.ok(overdue.warnings.some(w=>w.includes('No newer snapshot')));
});
test('active pointer and immutable member mismatches fail closed',async()=>{
  for(const mutate of [f=>f.files[f.prefix+'summary.json']+='\n',f=>f.files[f.prefix+'leaderboard.csv']+='\n']){
    const f=setup();mutate(f);f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
    const out=await f.consumer.call('active_rankings',{session:SESSION});
    assert.equal(out.status,'failed');assert.equal(out.errors.at(-1).code,'source_changed');
  }
});
test('pinned detail rejects changed source sweep and never mixes quote path',async()=>{
  const f=setup(),first=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  f.files[BASE+'sweeps/sweep_0001.jsonl.gz']=gzipSync('{"symbol":"'+CONTRACT+'"}\n');
  const out=await f.consumer.call('result_detail',{reference:first.rows[0].detail_reference,limit:2});
  assert.equal(out.status,'failed');assert.equal(out.errors[0].code,'source_changed');
  assert.equal(out.quote_path,undefined);
});
test('missing pinned source sweep is partial with an exact retry offset',async()=>{
  const f=setup(),first=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  delete f.files[BASE+'sweeps/sweep_0002.jsonl.gz'];
  const out=await f.consumer.call('result_detail',{reference:first.rows[0].detail_reference,limit:2});
  assert.equal(out.status,'partial');assert.equal(out.errors[0].code,'missing_artifact');
  assert.deepEqual(out.pagination.retry_offsets,[1]);assert.equal(out.quote_path.length,1);
});
test('active missing, before-open, holiday and invalid filters stay explicit',async()=>{
  const f=setup();delete f.files[ACTIVE+'latest.json'];
  assert.equal((await f.consumer.call('active_rankings',{session:SESSION})).status,'missing');
  f.consumer.now=()=>Date.parse('2026-10-02T12:00:00Z');
  assert.equal((await f.consumer.call('active_rankings',{session:'current'})).status,'before_open');
  assert.equal((await f.consumer.call('active_rankings',{session:'2026-10-03'})).status,'holiday');
  for(const a of [{session:'../secret'},{session:SESSION,sweep:10000},{session:SESSION,url:'https://example.com'},{session:SESSION,limit:51}]){
    const g=setup(),out=await g.consumer.call('active_rankings',a);
    assert.equal(out.status,'failed');assert.equal(g.calls.length,0);
  }
  assert.throws(()=>permitted(BASE+'inprogress/sweep_0006/secrets.json'),/allowlist/);
});
test('active detail and paging retain request and byte bounds',async()=>{
  const f=setup();const first=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  const detail=await f.consumer.call('result_detail',{reference:first.rows[0].detail_reference,limit:50,offset:3});
  assert.equal(detail.quote_path.length,2);assert.equal(detail.pagination.limit,2);
  assert.ok(f.calls.length<=LIMITS.requests*2);
});
test('newer failed attempt stays visible beside last successful snapshot',async()=>{
  const f=setup();f.files[ACTIVE+'latest-attempt.json']=JSON.stringify({schema_version:1,trade_date:SESSION,
    sweep:12,status:'failed',reason:'timeout',at:'2026-10-02T15:00:00Z'});
  const out=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  assert.equal(out.status,'partial');assert.equal(out.actual.sweep,6);
  assert.equal(out.attempt.status,'failed');assert.ok(out.warnings.some(w=>w.includes('timeout')));
});
test('a final daily result supersedes latest active only through the final producer path',async()=>{
  const f=setup();f.files[BASE+'summary.json']=fixtures()[BASE+'summary.json'];
  const out=await f.consumer.call('active_rankings',{session:SESSION,view:'clean',limit:1});
  assert.equal(out.status,'final');assert.equal(out.active_state,'final');assert.equal(out.rows.length,1);
  const detail=await f.consumer.call('result_detail',{reference:out.rows[0].detail_reference,limit:1});
  assert.equal(detail.status,'available');assert.equal(detail.actual.final,true);
});
test('early-close boundary marks a still-published snapshot as pending final',async()=>{
  const f=setup();f.summary.session_close_ms=Date.parse('2026-10-02T16:00:00Z');
  const revised=JSON.stringify(f.summary), revisedPrefix=ACTIVE+'sweep_0006/rev_'+hash(revised)+'/';
  for(const name of ['leaderboard.csv','qualified_ask_bid_v1.csv','manifest.jsonl'])f.files[revisedPrefix+name]=f.files[f.prefix+name];
  f.files[revisedPrefix+'summary.json']=revised;
  f.pointer.archive=revisedPrefix.slice(0,-1);
  f.pointer.members['summary.json']=hash(revised);
  f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
  f.consumer.now=()=>Date.parse('2026-10-02T16:10:00Z');
  const out=await f.consumer.call('active_rankings',{session:SESSION});
  assert.equal(out.status,'closed_pending_final');assert.equal(out.active_state,'closed_pending_final');
});
test('fresh cutoff with old ranked quote is stale and partial',async()=>{
  const f=setup();
  const now=Date.parse('2026-10-02T14:17:00Z');
  f.consumer.now=()=>now;
  f.summary.cutoff_ms=now-60000;
  f.summary.assessment.chain_status_counts={ok:6,error:4};
  const revised=JSON.stringify(f.summary), revisedPrefix=ACTIVE+'sweep_0006/rev_'+hash(revised)+'/';
  for(const name of ['leaderboard.csv','qualified_ask_bid_v1.csv','manifest.jsonl'])f.files[revisedPrefix+name]=f.files[f.prefix+name];
  f.files[revisedPrefix+'summary.json']=revised;
  f.pointer.archive=revisedPrefix.slice(0,-1);f.pointer.cutoff_ms=f.summary.cutoff_ms;
  f.pointer.members['summary.json']=hash(revised);f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
  const out=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  assert.equal(out.freshness.cutoff.status,'fresh');
  assert.equal(out.freshness.status,'stale');
  assert.equal(out.status,'partial');
  assert.ok(out.rows[0].ranked_evidence_freshness.age_seconds>600);
});
test('quote timestamp age is reported even when ranked sample time is recent',async()=>{
  const f=setup();f.consumer.now=()=>f.summary.cutoff_ms+60000;
  const lines=f.files[f.prefix+'leaderboard.csv'].trim().split('\n'),headers=lines[0].split(',');
  const cells=lines[1].split(',');
  cells[headers.indexOf('mid_first_to_max_exit_ms')]=String(f.summary.cutoff_ms-1000);
  cells[headers.indexOf('mid_first_to_max_exit_quote_age_s')]='1200';
  lines[1]=cells.join(',');const board=lines.join('\n')+'\n';
  f.summary.member_sha256['leaderboard.csv']=hash(board);
  const revised=JSON.stringify(f.summary), revisedPrefix=ACTIVE+'sweep_0006/rev_'+hash(revised)+'/';
  f.files[revisedPrefix+'leaderboard.csv']=board;
  for(const name of ['qualified_ask_bid_v1.csv','manifest.jsonl'])f.files[revisedPrefix+name]=f.files[f.prefix+name];
  f.files[revisedPrefix+'summary.json']=revised;
  f.pointer.archive=revisedPrefix.slice(0,-1);f.pointer.members['summary.json']=hash(revised);
  f.pointer.members['leaderboard.csv']=hash(board);f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
  const out=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  assert.equal(out.freshness.cutoff.status,'fresh');assert.equal(out.status,'partial');
  assert.equal(out.rows[0].ranked_evidence_freshness.age_seconds,1261);
});
test('explicit sweep follows its pinned revision pointer',async()=>{
  const f=setup();
  const out=await f.consumer.call('active_rankings',{session:SESSION,sweep:6,limit:1});
  assert.equal(out.actual.archive,f.pointer.archive);
  delete f.files[ACTIVE+'sweep_0006.json'];
  assert.equal((await f.consumer.call('active_rankings',{session:SESSION,sweep:6})).status,'missing');
});
test('pinned detail rejects out-of-sweep and future-timestamp quote evidence',async()=>{
  for(const change of [r=>{r.t-=1;},r=>{r.bid_ms=r.t+1;},r=>{r.ask_ms=Math.floor(r.t/1000);},
    r=>{r.bid_ms=0;},r=>{r.trade_ms=0;r.last=1;}]){
    const f=setup(),key=BASE+'sweeps/sweep_0001.jsonl.gz';
    const rows=gunzipSync(f.files[key]).toString().trim().split('\n').map(JSON.parse);
    change(rows[0]);f.files[key]=gzipSync(rows.map(r=>JSON.stringify(r)).join('\n')+'\n');
    f.manifest[0].sha256=hash(f.files[key]);
    const manifest=f.manifest.map(m=>JSON.stringify(m)).join('\n')+'\n';
    f.summary.member_sha256['manifest.jsonl']=hash(manifest);
    const revised=JSON.stringify(f.summary),revisedPrefix=ACTIVE+'sweep_0006/rev_'+hash(revised)+'/';
    f.files[revisedPrefix+'leaderboard.csv']=f.files[f.prefix+'leaderboard.csv'];
    f.files[revisedPrefix+'qualified_ask_bid_v1.csv']=f.files[f.prefix+'qualified_ask_bid_v1.csv'];
    f.files[revisedPrefix+'manifest.jsonl']=manifest;f.files[revisedPrefix+'summary.json']=revised;
    f.pointer.archive=revisedPrefix.slice(0,-1);f.pointer.members['summary.json']=hash(revised);
    f.pointer.members['manifest.jsonl']=hash(manifest);f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
    const ranking=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
    const detail=await f.consumer.call('result_detail',{reference:ranking.rows[0].detail_reference,limit:1});
    assert.equal(detail.status,'partial');assert.equal(detail.quote_path.length,0);
    assert.equal(detail.errors[0].code,'incompatible_schema');
  }
});
test('pinned detail treats published trade_ms zero as missing, not epoch evidence',async()=>{
  const f=setup(),key=BASE+'sweeps/sweep_0001.jsonl.gz';
  const rows=gunzipSync(f.files[key]).toString().trim().split('\n').map(JSON.parse);
  Object.assign(rows[0],{trade_ms:0,last:null});
  Object.assign(rows[1],{symbol:CONTRACT,type:'call',trade_ms:0,last:null,
    bid_ms:0,ask_ms:0,bid:null,ask:null});
  f.files[key]=gzipSync(rows.map(r=>JSON.stringify(r)).join('\n')+'\n');
  f.manifest[0].sha256=hash(f.files[key]);
  const manifest=f.manifest.map(m=>JSON.stringify(m)).join('\n')+'\n';
  f.summary.member_sha256['manifest.jsonl']=hash(manifest);
  const revised=JSON.stringify(f.summary),revisedPrefix=ACTIVE+'sweep_0006/rev_'+hash(revised)+'/';
  for(const name of ['leaderboard.csv','qualified_ask_bid_v1.csv'])f.files[revisedPrefix+name]=f.files[f.prefix+name];
  f.files[revisedPrefix+'manifest.jsonl']=manifest;f.files[revisedPrefix+'summary.json']=revised;
  f.pointer.archive=revisedPrefix.slice(0,-1);f.pointer.members['summary.json']=hash(revised);
  f.pointer.members['manifest.jsonl']=hash(manifest);f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
  const ranking=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  const detail=await f.consumer.call('result_detail',{reference:ranking.rows[0].detail_reference,limit:1});
  assert.equal(detail.status,'in_progress');
  assert.equal(detail.quote_path[0].trade_ms,null);
  assert.ok(detail.quote_path[0].bid_ms>0);
  for(const field of ['bid_ms','ask_ms','trade_ms'])assert.equal(detail.quote_path[1][field],null);
});
test('MCP discovery, active query and pinned detail use read-only bounded schemas',async()=>{
  const f=setup(),p=new Protocol(f.consumer);
  assert.ok(tools.find(t=>t.name==='active_rankings').annotations.readOnlyHint);
  await p.handle({jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-11-25',
    capabilities:{},clientInfo:{name:'active-fixture',version:'1'}}});
  await p.handle({jsonrpc:'2.0',method:'notifications/initialized'});
  const call=async(name,args)=>(await p.handle({jsonrpc:'2.0',id:2,method:'tools/call',
    params:{name,arguments:args}})).result.structuredContent;
  const discovery=await call('discover_sources',{session:SESSION});
  assert.equal(discovery.capabilities.at(-1).dataset,'options_active');
  const ranking=await call('active_rankings',{session:SESSION,limit:1});
  const detail=await call('result_detail',{reference:ranking.rows[0].detail_reference,limit:1});
  assert.equal(detail.actual.archive,ranking.actual.archive);
  assert.equal(detail.quote_path[0].sweep,1);
});
// Review of 07d61d0 (P2): a pinned old sweep must not claim nothing newer exists.
test('pinned old sweep reports its own age separately from a newer latest snapshot',async()=>{
  const f=setup();const published=Date.parse(f.pointer.published_at);
  f.consumer.now=()=>published+2701000;
  const latest={...f.pointer,sweep:12,published_at:new Date(published+2600000).toISOString()};
  f.files[ACTIVE+'latest.json']=JSON.stringify(latest);
  const pinned=await f.consumer.call('active_rankings',{session:SESSION,sweep:6,limit:1});
  assert.equal(pinned.actual.sweep,6);assert.equal(pinned.freshness.status,'stale');
  assert.deepEqual(pinned.newer_snapshot,{available:true,sweep:12,published_at:latest.published_at});
  assert.notEqual(pinned.cadence.status,'overdue');assert.equal(pinned.cadence.seconds_since_publication,101);assert.equal(pinned.cadence.since_publication_of.sweep,12);
  assert.ok(!pinned.warnings.some(w=>w.includes('No newer snapshot')));
  assert.ok(pinned.warnings.some(w=>w.includes('Pinned sweep 6 is not the newest snapshot; sweep 12')));
  // Pinned to the latest sweep with nothing newer: overdue still reported.
  f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
  const current=await f.consumer.call('active_rankings',{session:SESSION,sweep:6,limit:1});
  assert.equal(current.newer_snapshot.available,false);assert.equal(current.cadence.status,'overdue');
  assert.ok(current.warnings.some(w=>w.includes('No newer snapshot')));
  // Latest pointer unreadable: availability is unknown, never "none newer".
  delete f.files[ACTIVE+'latest.json'];
  const unknown=await f.consumer.call('active_rankings',{session:SESSION,sweep:6,limit:1});
  assert.equal(unknown.newer_snapshot.available,null);assert.notEqual(unknown.cadence.status,'overdue');
  assert.ok(!unknown.warnings.some(w=>w.includes('No newer snapshot')));
});
