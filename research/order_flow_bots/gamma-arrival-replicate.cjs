'use strict';
// Make the timing comparison reproducible without the private runner DB or a mutable tape.
const fs=require('node:fs'), path=require('node:path');
const {createHash}=require('node:crypto');
const {gzipSync,gunzipSync}=require('node:zlib');
const {at,compare,readInputs,keyFields,confirmedMs,stats}=require('./gamma-arrival-study.cjs');
const digest=b=>createHash('sha256').update(b).digest('hex');
const evidenceDir=path.join(__dirname,'gamma-arrival-evidence');
const fields=['chain_sequence','snapshot_key','rule','published_ms','next_boundary_ms','runner_decision_ms','arrival_spot','boundary_spot','arrival_signal','boundary_signal','runner_signal','arrival_reason','boundary_reason','runner_reason','arrival_next_quote_ms','arrival_bid','arrival_ask','boundary_next_quote_ms','boundary_bid','boundary_ask'];
function csv(result) {
  const lines=[fields];
  for(const r of result.pairs) lines.push([r.sequence,r.key,r.id,r.arrival,r.boundary,r.productionEvaluated,r.aSpot,r.bSpot,
    r.aSignal,r.bSignal,r.productionSignal,r.aReason,r.bReason,r.productionReason,
    ...[r.aQuote,r.bQuote].flatMap(q=>[q?.received_at,q?.bid,q?.ask])]);
  const escape=x=>{const s=x==null?'':String(x);return /[,"\n]/.test(s)?'"'+s.replaceAll('"','""')+'"':s;};
  return lines.map(row=>row.map(escape).join(',')).join('\n')+'\n';
}
function selected(rows,instants) {
  // Keep the immediately prior prefix plus its potentially ambiguous boundary bucket.
  const retained=new Set();
  for(const t of instants) for(const cutoff of [t-1,t]) {
    const r=at(rows,cutoff);
    if(r)retained.add(r);
  }
  return [...retained].sort((a,b)=>a.at-b.at);
}
function pack(inputs) {
  const {day,open,stopIso,decisions,frames,ledgerQuotes,ladder,chains,tape}=inputs;
  const instants=new Set(), stop=Date.parse(stopIso);
  for(const c of chains) if(c.at>=open && c.at<=stop) {
    for(const arm of [c.at,Math.ceil(c.at/60000)*60000]) {
      for(let k=0; k<=32 && arm-k*60000>=open-60000;k++) instants.add(arm-k*60000);
    }
  }
  const flow=ladder.map(({symbol})=>selected(tape.rowsBySymbol.get(symbol),instants)
    .map(r=>[r.at,...keyFields.map(k=>r[k])]));
  return {schema:1,day,open,stopIso,decisions,ledgerQuotes,ladder,
    frames:frames.map(f=>({t:f.t,chainTime:f.chainTime??null,symbols:f.contracts.map(c=>c.symbol)})),
    chains:chains.map(c=>({at:c.at,captureMs:c.captureMs,key:c.key,sequence:c.sequence,digest:c.digest,
      gamma:ladder.map(({symbol})=>c.gamma.get(symbol)??null)})),
    tape:{flow,quotes:selected(tape.quotes,instants).map(({at,bidAt,askAt,bid,ask})=>({at,bidAt,askAt,bid,ask})),
      gaps:tape.gaps,duplicated:tape.duplicated,nPrints:tape.nPrints,last:tape.last,digest:tape.digest}};
}
function unpack(b) {
  if(b.schema!==1 || b.tape.flow.length!==b.ladder.length)throw Error('Unknown evidence format');
  const symbols=b.ladder.map(c=>c.symbol);
  return {...b,frames:b.frames.map(f=>({...f,contracts:f.symbols.map(symbol=>({symbol}))})),
    chains:b.chains.map(c=>({...c,gamma:new Map(symbols.map((s,i)=>[s,c.gamma[i]]))})),
    tape:{...b.tape,rowsBySymbol:new Map(symbols.map((s,i)=>[s,b.tape.flow[i].map(([at,...v])=>({
      at,...Object.fromEntries(keyFields.map((k,j)=>[k,v[j]]))}))]))}};
}
function watcherStats(records) {
  const lag=[],other=[];
  for(const x of records) {
    if(!x.receipt || x.receipt.snapshot_key!==x.key || x.receipt.publication.status!=='published' ||
      x.receipt.one_clock_publication.stream!=='collector-chain/QQQ' ||
      x.receipt.one_clock_publication.sequence!==x.sequence ||
      x.quote?.symbol!=='QQQ' || !(x.quote.bid>0 && x.quote.ask>=x.quote.bid) ||
      x.quote.receivedAt>x.decisionAt || x.flowAt>x.decisionAt || x.received>x.decisionAt ||
      x.received<confirmedMs(x.receipt.latest_published_at)) throw Error('Invalid watcher receipt '+x.key);
    lag.push(x.received-confirmedMs(x.receipt.latest_published_at));
    other.push(x.decisionAt-x.received);
  }
  return {subscriberLagMs:stats(lag),subsequentReadsMs:stats(other)};
}
function replicate(out) {
  const manifest=JSON.parse(fs.readFileSync(path.join(evidenceDir,'files.json')));
  for(const [file,sha] of Object.entries(manifest)) {
    if(digest(fs.readFileSync(path.join(evidenceDir,file)))!==sha) throw Error('Evidence digest mismatch: '+file);
  }
  const bundle=JSON.parse(gunzipSync(fs.readFileSync(path.join(evidenceDir,'inputs.json.gz'))));
  if(digest(fs.readFileSync(path.join(evidenceDir,'tape-manifest.json')))!==bundle.tape.digest) throw Error('Tape manifest mismatch');
  const receipts=JSON.parse(fs.readFileSync(path.join(evidenceDir,'chain-receipts.json')));
  if(receipts.length!==bundle.chains.length) throw Error('Incomplete chain provenance');
  for(const [i,chain] of bundle.chains.entries()) {
    const record=receipts[i];
    if(record.key!==chain.key || record.sequence!==chain.sequence || record.payloadSha256!==chain.digest ||
      confirmedMs(record.latest_published_at)!==chain.at || confirmedMs(record.capture)!==chain.captureMs) {
      throw Error('Chain receipt does not match retained inputs');
    }
  }
  for(const q of bundle.ledgerQuotes) {
    if(q.t!==Math.min(q.bid_at,q.ask_at) || ![q.bid_at,q.ask_at,q.bid_through,q.ask_through].every(Number.isSafeInteger) ||
      q.bid_at>q.bid_through || q.ask_at>q.ask_through || Math.max(q.bid_through,q.ask_through)>q.received_at ||
      q.received_at-q.t>15000 || !(q.bid>0 && q.ask>=q.bid)) throw Error('Invalid executable quote');
  }
  const result=compare(unpack(bundle)),generated=csv(result);
  const expected=fs.readFileSync(path.join(__dirname,'gamma-arrival-20261124.csv'),'utf8');
  if(generated!==expected) throw Error('Regenerated CSV differs from committed evidence');
  if(out) fs.writeFileSync(out,generated,{flag:'wx'});
  console.log(JSON.stringify({csvSha256:digest(generated),summary:result.summary,
    watcher:watcherStats(JSON.parse(fs.readFileSync(path.join(evidenceDir,'watcher.json'))))},null,2));
}
if(require.main===module) {
  const [mode,...args]=process.argv.slice(2);
  if(mode==='pack') {
    const input=readInputs(args), b=pack(input);
    if(csv(compare(unpack(b)))!==csv(compare(input))) throw Error('Sparse evidence lost a rule input');
    fs.writeFileSync(args[5],gzipSync(JSON.stringify(b),{level:9}),{flag:'wx'});
    fs.writeFileSync(args[6],csv(compare(input)),{flag:'wx'});
  } else if(mode==='verify') replicate(args[0]);
  else throw Error('Use verify [new.csv] or pack DB DAY CHAINS TAPE CUTOFF new.json.gz new.csv');
}
module.exports={pack,unpack,csv,watcherStats};
