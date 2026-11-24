'use strict';
// Read-only shadow: notice a newly published chain, then acquire the current
// cumulative flow and retain the exact receipts used in an isolated JSONL.
const fs = require('node:fs');
const { setTimeout: sleep } = require('node:timers/promises');
const { FLOW_URL, CHAIN_URL } = require('./forward-feed.cjs');
const { fetchQqqQuote } = require('./forward-quote.cjs');
async function watch(filename,minutes) {
  if(!Number.isInteger(minutes)||minutes<1||minutes>390) throw Error('Expected 1 to 390 minutes');
  const file=fs.openSync(filename,'wx',0o600);
  const until=Date.now()+minutes*60000;
  let previous=null;
  try {
    while(Date.now()<until) {
      try {
        const chain=await get(CHAIN_URL);
        if(chain.body.snapshot_key!==previous) {
          const first=previous===null;
          previous=chain.body.snapshot_key;
          const [flow,quote]=await Promise.all([get(FLOW_URL),fetchQqqQuote()]);
          const s=flow.body.session;
          fs.writeSync(file,JSON.stringify({first,session:chain.body.date,key:previous,received:chain.received,
            decisionAt:Date.now(),flowAt:flow.received,flowDay:s.day,firstPrint:s.first,lastPrint:s.last,
            gaps:s.gaps,rebuilt:s.rebuilt,subscribed:Object.keys(s.opening||{}),totals:s.contracts,quote,
            rows:chain.body.rows.map(r=>({symbol:r.OptionSymbol,gamma:r.Gamma}))})+'\n');
        }
      } catch(e) {console.error(e.message);}
      await sleep(3000);
    }
  } finally {fs.closeSync(file);}
}
async function get(url) {
  const response=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(5000)});
  if(!response.ok) throw Error('HTTP '+response.status);
  const body=await response.json();
  return {body,received:Date.now()};
}
if(require.main===module) watch(process.argv[2],Number(process.argv[3])).catch(e=>{console.error(e.message);process.exitCode=1;});
