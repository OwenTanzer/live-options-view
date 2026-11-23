const assert = require('node:assert/strict');
(async()=>{
 const {settlementMark,fetchSpotMarkForDate,handleSettle,settleAllBots}=await import('../backend/settlement.js');
 const {computeBookFromTrades,SESSION_COOKIE,BOT_INDEX_KEY}=await import('../backend/common.js');
 const {exactExecutionQuote}=await import('../backend/execution.js');
 const exp='2026-11-06';
 const trade=(root,strike,type='call')=>({sym:`${root}261106${type==='call'?'C':'P'}${String(strike*1000).padStart(8,'0')}`,strike,type,exp,side:'sell',qty:1,price:1,execution_request_id:root+type});
 const initial={username:'fixture',is_bot:true,version:1,balance_cash:100000,trades:[trade('QQQ',600),trade('SPY',700),trade('IBIT',40),trade('IBIT',60,'put')]};
 const {withD1}=await import('./helpers/test_env.mjs');
 const {createStorage}=await import('../backend/storage.js');
 const env=withD1({PAPER_TRADES:{get:async()=>({json:async()=>({schema_version:1,ready:true,members:['fixture']})})}});
 const storage=createStorage(env);
 const current=async()=>storage.users.get('fixture');
 await storage.users.insert('fixture',structuredClone(initial));
 await storage.sessions.create('test','fixture',3600);
 assert.equal(computeBookFromTrades(initial.trades)[initial.trades[1].sym].underlying,'SPY');
 assert.equal(settlementMark({underlying:'SPY',exp},{[exp]:610}),null);
 assert.equal(settlementMark({exp},{[exp]:610}),610);
 let ibitAvailable=false;
 const original=global.fetch;
 const seen=[];
 global.fetch=async url=>{seen.push(String(url));if(String(url).includes('qqq_chain'))return new Response(`UnderlyingPrice,Expiration\n610,${exp}\n`);const root=String(url).includes('/spy/')?'SPY':'IBIT';return Response.json({symbol:root,expiration:exp,date:exp,spot:root==='SPY'?725:50,source:'dxlink',price_basis:'first_observed_post_close',observed_at:exp+'T21:00:01Z'},{status:root==='IBIT'&&!ibitAvailable?404:200});};
 try{
  await settleAllBots(env,new Date('2026-11-07T12:00:00Z'));
  let record=await current();assert.equal(record.trades.length,6);assert.equal(record.balance_cash,96500);
  assert.ok(seen.some(u=>u.includes('/settlement/spy/')));assert.ok(seen.some(u=>u.includes('/settlement/ibit/')));
  ibitAvailable=true; await settleAllBots(env,new Date('2026-11-07T12:00:00Z'));
  record=await current();assert.equal(record.trades.length,8);assert.equal(record.balance_cash,94500);
  await settleAllBots(env,new Date('2026-11-07T12:00:00Z')); record=await current();assert.equal(record.trades.length,8);
  await storage.users.put('fixture',structuredClone(initial));
  const req=new Request('https://live-options-view.otmooper12.workers.dev/api/settle',{method:'POST',headers:{Cookie:`${SESSION_COOKIE}=test`,'Content-Type':'application/json'},body:JSON.stringify({as_of:'2026-11-07',spot_marks:{[exp]:610,['SPY:'+exp]:99999,['IBIT:'+exp]:99999}})});
  const res=await handleSettle(req,env); assert.equal(res.status,200);record=await current();assert.equal(record.balance_cash,94500,'browser uses durable own-underlying marks, not supplied foreign values');
  global.fetch=async()=>Response.json({symbol:'QQQ',date:exp,expiration:exp,spot:500,observed_at:exp+'T21:00:01Z'});
  assert.equal(await fetchSpotMarkForDate(exp,'IBIT'),null);
  global.fetch=async()=>Response.json({symbol:'IBIT',date:exp,expiration:exp,spot:50,source:'dxlink',price_basis:'first_observed_post_close',observed_at:exp+'T15:00:00Z'});
  assert.equal(await fetchSpotMarkForDate(exp,'IBIT'),null,'a midday observation is not a post-close mark');
  global.fetch=async()=>Response.json({symbol:'IBIT',date:exp,expiration:exp,spot:50,source:'dxlink',price_basis:'first_observed_post_close',observed_at:exp+'T21:30:00Z'});
  assert.equal(await fetchSpotMarkForDate(exp,'IBIT'),null,'an observation after the 16:15 ET window is not a post-close mark');
 }finally{global.fetch=original}
 for(const root of ['QQQ','SPY','IBIT'])for(const type of ['call','put'])for(const side of ['buy','sell']){
  const t=trade(root,50,type),now=Date.now();const row={symbol:t.sym,bid:1,ask:1.2,strike:50,type,exp,bid_ts:new Date(now).toISOString(),ask_ts:new Date(now).toISOString()};
  assert.equal(exactExecutionQuote({quotes:[row]},{sym:t.sym,side},now).error,undefined);
  assert.ok(exactExecutionQuote({quotes:[{...row,exp:'2026-11-13'}]},{sym:t.sym,side},now).error);
  assert.ok(exactExecutionQuote({quotes:[{...row,bid_ts:null,ask_ts:null}]},{sym:t.sym,side},now).error);
  assert.ok(exactExecutionQuote({quotes:[row]},{sym:t.sym,side},now+60000).error);
 }
 console.log('PASS mixed QQQ/SPY/IBIT browser + cron settlement, missing marks, repeat, legacy, exact quote identities');
})().catch(e=>{console.error(e);process.exitCode=1});

