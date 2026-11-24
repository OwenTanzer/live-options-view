'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {gunzipSync} = require('node:zlib');
const {createHash} = require('node:crypto');
const {DatabaseSync} = require('node:sqlite');
const {ofStrike} = require('../../site/shared/order-flow-exposure.js');
const evaluate = require('./strategies-a.cjs');
const {timeBounds} = require('./forward-quote.cjs');
const MIN=60000, IDS=[1,4,5];
const keyFields=['buy','sell','unknown','buyPremium','sellPremium','unknownPremium','printCount','unknownCount'];
const zeros=()=>Object.fromEntries(keyFields.map(k=>[k,0]));
const hash=b=>createHash('sha256').update(b).digest('hex');
// Entries are sorted by actual availability, including same-millisecond ties.
function at(rows,t) {
  let lo=0,hi=rows.length;
  while(lo<hi) {const mid=(lo+hi)>>>1;if(rows[mid].at<=t) lo=mid+1;else hi=mid;}
  return rows[lo-1];
}
function readTape(dir,ladder,open) {
  const manifest=JSON.parse(fs.readFileSync(path.join(dir,'manifest.json')));
  const rowsBySymbol=new Map(ladder.map(c=>[c.symbol,[]]));
  const quotes=[],gaps=[],seen=new Map();
  let duplicated=0;
  for(const [i,part] of manifest.entries()) {
    if(part.minute!==open+i*MIN) throw Error('Noncontiguous manifest');
    if(part.missing) {gaps.push(part.minute);continue;}
    const raw=gunzipSync(fs.readFileSync(path.join(dir,part.minute+'.json.gz')));
    if(hash(raw)!==part.sha256) throw Error('Minute digest mismatch');
    const data=JSON.parse(raw);let last=part.minute;
    for(const r of data.rows) {
      if(r.at<last || r.at>=part.minute+MIN) throw Error('Rows outside monotonic receipt window');
      last=r.at;
      if(r.kind==='gap') {gaps.push(r.at);continue;}
      if(r.kind==='quote' && Number.isFinite(r.bid) && Number.isFinite(r.ask) && r.bid>0 && r.ask>=r.bid &&
        [r.bidAt,r.askAt].every(x=>Number.isSafeInteger(x) && x<=r.at && r.at-x<=15000)) {
        quotes.push(r);continue;
      }
      if(r.kind!=='option' || !rowsBySymbol.has(r.symbol)) continue;
      if(!['buy','sell','unknown'].includes(r.side) || !Number.isSafeInteger(r.seq) ||
        !Number.isSafeInteger(r.size) || r.size<=0 || !Number.isFinite(r.price) || r.price<=0) throw Error('Malformed option print');
      const id=r.symbol+'/'+r.seq, fingerprint=[r.t,r.size,r.price,r.side].join('/');
      if(seen.has(id)) {if(seen.get(id)!==fingerprint)throw Error('Conflicting print identity');duplicated++;continue;}
      seen.set(id,fingerprint);
      const arr=rowsBySymbol.get(r.symbol), sum={...(arr.at(-1)||zeros()),at:r.at};
      sum[r.side]+=r.size;sum[r.side+'Premium']+=r.size*r.price*100;sum.printCount++;if(r.side==='unknown')sum.unknownCount++;
      arr.push(sum);
    }
  }
  return {quotes,rowsBySymbol,gaps,duplicated,nPrints:seen.size,last:manifest.at(-1).minute+MIN-1,
    digest:hash(fs.readFileSync(path.join(dir,'manifest.json')))};
}
function frameAt(t,open,tape,chains,ladder) {
  // Tape 'at' is truncated. Only a bucket ending by the decision instant is known.
  const q=at(tape.quotes,t-1), chain=at(chains,t);
  const spot=q && t-Math.min(q.bidAt,q.askAt)<=15000?(q.bid+q.ask)/2:null;
  // A possible loss at the endpoint vetoes a decision conservatively, even
  // though an ambiguous positive observation never contributes to the signal.
  const complete=t>=open && t<=tape.last && spot!==null && !tape.gaps.some(g=>g<=t);
  const contracts=ladder.map(c=>{
    const arr=tape.rowsBySymbol.get(c.symbol),end=at(arr,t-1)||zeros(),start=at(arr,t-MIN-1)||zeros();
    const minute=Object.fromEntries(keyFields.map(k=>[k,end[k]-start[k]]));
    return {...c,...minute,spot,gamma:chain?.gamma.get(c.symbol)??null,flow:{B:end.buy,S:end.sell,U:end.unknown}};
  });
  const strikes=[];
  if(chain && t-chain.captureMs<=120000 && contracts.every(c=>Number.isFinite(c.gamma) && c.gamma>=0)) {
    const ks=[...new Set(contracts.map(c=>c.strike))];
    for(const k of ks) strikes.push(ofStrike(k,contracts.filter(c=>c.strike===k)));
  }
  return {t,spot,complete,minuteComplete:complete,contracts,strikes,chainKey:chain?.key};
}
// The preceding range and pressure reference windows move with the endpoint.
// Thirty-one trailing minutes cover all three named rules.
function runArm(t,open,tape,chains,ladder) {
  const history=[];
  for(let i=31;i>=0;i--) if(t-i*MIN>=open) history.push(frameAt(t-i*MIN,open,tape,chains,ladder));
  const frame=history.at(-1);
  if(!frame?.complete || !frame.strikes.length) return {eligible:false,reason:'incomplete tape, BBO, or chain'};
  return {eligible:true,spot:frame.spot,chainKey:frame.chainKey,
    rules:evaluate(history).filter(r=>IDS.includes(r.id))};
}
function fillOpportunity(quotes,decision,cutoff) {
  return quotes.find(q=>q.t>decision && q.received_at>decision && q.received_at<=Math.min(cutoff,decision+120000));
}
function stats(a) {
  a=a.filter(Number.isFinite).sort((x,y)=>x-y);
  const quant=p=>{const i=(a.length-1)*p,v=a[Math.floor(i)];return a.length?Math.round(v+(a[Math.ceil(i)]-v)*(i%1)):null;};
  return {n:a.length,min:a[0]??null,median:quant(.5),p90:quant(.9),max:a.at(-1)??null};
}
function confirmedMs(s) {
  // Date.parse truncates submilliseconds. Round receipt up to avoid using it early.
  const parsed=timeBounds(s);if(!parsed) throw Error('Invalid dated timestamp');
  return parsed.upper;
}
function readInputs([dbPath,day,archive,dir,stopIso]) {
  const stop=Date.parse(stopIso);
  if(!Number.isFinite(stop)) throw Error('Specify immutable UTC cutoff');
  const db=new DatabaseSync(dbPath,{readOnly:true});db.exec('BEGIN');
  const {open}=db.prepare('SELECT open FROM sessions WHERE session=?').get(day);
  const decisions=db.prepare('SELECT t,id,evaluated_at,signal,reason FROM decisions WHERE session=? AND evaluated_at<=? AND id IN (1,4,5) ORDER BY t,id').all(day,stop);
  const frames=db.prepare('SELECT t,body FROM frames WHERE session=? AND t<=? ORDER BY t').all(day,stop).map(x=>JSON.parse(x.body));
  const ledgerQuotes=db.prepare('SELECT t,received_at,bid,ask,bid_at,ask_at,bid_through,ask_through FROM quotes WHERE received_at BETWEEN ? AND ? ORDER BY received_at').all(open,stop);
  db.exec('COMMIT');db.close();
  const first=frames.find(f=>f.contracts?.length===16);if(!first) throw Error('Missing fixed ladder');
  const ladder=first.contracts.map(({symbol,strike,type})=>({symbol,strike,type}));
  const chains=fs.readFileSync(archive,'utf8').trim().split('\n').map(JSON.parse).filter(r=>!r.excluded)
    .map(r=>({...r,at:confirmedMs(r.arrival),captureMs:confirmedMs(r.capture),gamma:new Map(r.rows.map(c=>[c.symbol,c.gamma]))})).sort((a,b)=>a.at-b.at);
  const tape=readTape(dir,ladder,open);
  return {day,open,stopIso,decisions,frames,ledgerQuotes,ladder,chains,tape};
}
function compare({day,open,stopIso,decisions,frames,ledgerQuotes,ladder,chains,tape}) {
  const stop=Date.parse(stopIso);
  const byMinute=new Map(frames.map(f=>[f.t,f]));
  const pairs=[],excluded={};
  const exclude=why=>{excluded[why]=(excluded[why]||0)+1;};
  for(const chain of chains) {
    if(chain.captureMs<open || chain.at>stop) continue;
    const boundary=Math.ceil(chain.at/MIN)*MIN;
    if(boundary>stop) {exclude('boundary beyond cutoff');continue;}
    if(at(chains,boundary)!==chain) {exclude('superseded before next boundary');continue;}
    const production=decisions.filter(x=>x.t===boundary);
    const f=byMinute.get(boundary);
    if(production.length!==3 || !f) {exclude('boundary not recorded by cutoff');continue;}
    if(f.contracts.length && f.contracts.map(c=>c.symbol).join()!==ladder.map(c=>c.symbol).join()) {exclude('changed production ladder');continue;}
    const a=runArm(chain.at,open,tape,chains,ladder),b=runArm(boundary,open,tape,chains,ladder);
    if(!a.eligible||!b.eligible) {exclude('incomplete '+(!a.eligible?'arrival':'boundary'));continue;}
    const qa=fillOpportunity(ledgerQuotes,chain.at,stop),qb=fillOpportunity(ledgerQuotes,boundary,stop);
    for(const id of IDS) {
      const ar=a.rules.find(r=>r.id===id),br=b.rules.find(r=>r.id===id),prod=production.find(r=>r.id===id);
      pairs.push({sequence:chain.sequence,key:chain.key,digest:chain.digest,id,arrival:chain.at,boundary,
        chainDelay:chain.at-chain.captureMs,lead:boundary-chain.at,
        productionEvaluated:prod.evaluated_at,productionUsedPairedChain:f.chainTime===chain.captureMs,
        aSpot:a.spot,bSpot:b.spot,aSignal:ar.signal,bSignal:br.signal,productionSignal:prod.signal,
        aReason:ar.reason,bReason:br.reason,productionReason:prod.reason,
        aQuote:qa||null,bQuote:qb||null});
    }
  }
  const anchors=pairs.filter(p=>p.id===1);
  const summary={day,cutoff:stopIso,ladder:ladder.map(c=>c.symbol),tapeManifestSha256:tape.digest,
    distinctOptionPrints:tape.nPrints,duplicatePrints:tape.duplicated,archiveGaps:tape.gaps.length,
    anchors:anchors.length,excluded,productionSameChain:anchors.filter(p=>p.productionUsedPairedChain).length,
    chainPublishMs:stats(anchors.map(p=>p.chainDelay)),arrivalLeadMs:stats(anchors.map(p=>p.lead)),
    arrivalNextQuoteMs:stats(anchors.map(p=>p.aQuote?p.aQuote.received_at-p.arrival:null)),
    boundaryNextQuoteMs:stats(anchors.map(p=>p.bQuote?p.bQuote.received_at-p.boundary:null)),
    quoteAvailabilityLeadMs:stats(anchors.filter(p=>p.aQuote && p.bQuote).map(p=>p.bQuote.received_at-p.aQuote.received_at)),
    rules:IDS.map(id=>{const rr=pairs.filter(p=>p.id===id);return {id,arrivals:rr.filter(p=>p.aSignal).length,boundaries:rr.filter(p=>p.bSignal).length,
      sameDirection:rr.filter(p=>p.aSignal && p.aSignal===p.bSignal).length,differingSignals:rr.filter(p=>p.aSignal!==p.bSignal).length,
      differingReasons:rr.filter(p=>p.aReason!==p.bReason).length,productionSignals:rr.filter(p=>p.productionSignal).length,
      boundaryProductionDifferences:rr.filter(p=>p.bSignal!==p.productionSignal).length};})};
  return {summary,pairs};
}
module.exports={at,frameAt,runArm,fillOpportunity,stats,confirmedMs,readInputs,compare,keyFields};
if(require.main===module) {
  const result=compare(readInputs(process.argv.slice(2)));
  fs.writeFileSync(process.argv[7],JSON.stringify(result,null,2)+'\n',{flag:'wx'});
  console.log(JSON.stringify(result.summary,null,2));
}
