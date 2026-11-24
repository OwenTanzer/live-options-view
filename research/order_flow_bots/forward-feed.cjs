'use strict';
const {timeBounds}=require('./forward-quote.cjs');
const {parseSymbol} = require('../../site/shared/order-flow-model.js');
const {ofStrike,OF_SNAPSHOT_MAX_AGE_MS} = require('../../site/shared/order-flow-exposure.js');

const ORIGIN = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const FLOW_URL = ORIGIN+'/order-flow/qqq/v1/recent.json';
const CHAIN_URL = ORIGIN+'/intraday/latest.json';
const MINUTE = 60000;
const MAX_DELAY = 15000;
const nonnegative = n => Number.isSafeInteger(n) && n >= 0;
const positive = n => Number.isFinite(n) && n > 0;
const keyOf = r => r.symbol+'/'+r.seq;
const sideField = {buy:'B',sell:'S',unknown:'U'};

function validRow(r) {
  return r && parseSymbol(r.symbol) && nonnegative(r.seq) && Number.isSafeInteger(r.t) &&
    positive(r.price) && nonnegative(r.size) && r.size > 0 && Object.hasOwn(sideField,r.side);
}

function ladder(day,spot) {
  const contracts=[];
  for(let strike=Math.floor(spot)-3;strike<=Math.floor(spot)+4;strike++) {
    for(const type of ['C','P']) {
      const symbol='QQQ'+day.slice(2).replaceAll('-','')+type+String(strike*1000).padStart(8,'0');
      contracts.push({symbol,strike,type});
    }
  }
  return contracts;
}

// The recent projection's rows are in collector insertion order. Provider
// timestamps alone do not establish continuity. The session counter and the
// ordered overlap together prove that no intervening accepted row fell off.
function suffix(previous, payload) {
  const rows=payload.rows, n=payload.session.prints, cap=payload.cap;
  if(!nonnegative(n) || !Number.isSafeInteger(cap) || cap<=0 || !Array.isArray(rows) ||
     rows.length>cap || rows.length>n || rows.some(r=>!validRow(r)) ||
     new Set(rows.map(keyOf)).size!==rows.length) return null;
  if(!previous) return {added:rows,continuous:n===rows.length};
  const delta=n-previous.count;
  if(delta<0 || delta>rows.length) return {added:rows,continuous:false};
  const overlap=rows.slice(0,rows.length-delta);
  const tail=previous.rows.slice(previous.rows.length-overlap.length);
  const matches=tail.length===overlap.length && overlap.every((r,i)=>{
    const old=tail[i];
    return keyOf(r)===keyOf(old) && r.t===old.t && r.side===old.side && r.price===old.price && r.size===old.size;
  });
  return {added:delta?rows.slice(-delta):[],continuous:matches};
}

// An unchanged counter must not hide a rebuilt/corrected contract projection.
function sameTotals(previous,current,added) {
  if(!previous || !current) return false;
  const delta=new Map();
  for(const row of added) {
    let flow=delta.get(row.symbol);
    if(!flow) {flow={B:0,S:0,U:0,prints:0};delta.set(row.symbol,flow);}
    flow[sideField[row.side]]+=row.size;flow.prints++;
  }
  for(const symbol of new Set([...Object.keys(previous),...Object.keys(current),...delta.keys()])) {
    const before=previous[symbol],now=current[symbol],expected=delta.get(symbol);
    if(!now || ['B','S','U','prints'].some(k=>!nonnegative(now[k]) ||
       now[k]-(before?.[k]??0)!==(expected?.[k]??0))) return false;
  }
  return true;
}

// Preserve sub-millisecond boundaries from the collector's ISO timestamps.
// Date.parse alone truncates a side at 09:30:00.000500 back to the open.
function stamp(value) {
  return timeBounds(value)?.upper??NaN;
}

function spotObservation(chain,ct,receivedAt) {
  const m=chain.underlying_market, p=m?.provenance;
  if(m?.symbol!=='QQQ' || m.source!=='dxlink' || m.freshness!=='live' ||
     p?.freshness!=='live' || p.price!==m.spot || !positive(m.spot) ||
     !['bid_ask_midpoint','last_trade'].includes(p.price_basis)) return null;
  const collector=stamp(p.collector_received_at);
  if(!Number.isFinite(collector) || stamp(m.spot_ts)!==collector || collector>ct || ct>receivedAt) return null;
  const legs=p.contributing;
  const midpoint=p.price_basis==='bid_ask_midpoint';
  const sides=midpoint?['bid','ask']:['last'];
  const receipts=[], ages=[];
  for(const side of sides) {
    const arrival=stamp(legs?.[side+'_ts']);
    if(!Number.isFinite(arrival) || arrival>ct) return null;
    receipts.push(arrival);
    const raw=legs[side+'_observed_at'];
    const source=raw==null?arrival:stamp(raw);
    if(!Number.isFinite(source) || source>arrival) return null;
    ages.push(timeBounds(raw??legs[side+'_ts']).lower);
  }
  if(Math.min(...receipts)!==collector) return null;
  const provider=p.provider_observed_at==null?null:stamp(p.provider_observed_at);
  if(provider!==null && (!Number.isFinite(provider) || provider>Math.max(...receipts))) return null;
  // The later receipt establishes when the whole value existed. The older
  // observation establishes freshness. Each governs a different decision.
  return {symbol:'QQQ',spot:m.spot,t:Math.ceil(Math.max(...receipts)),
    oldest:Math.floor(Math.min(...ages,timeBounds(p.provider_observed_at)?.lower??Infinity)),receivedAt};
}

function usableBbo(q,now) {
  if(q?.symbol!=='QQQ' || !positive(q.bid) || !positive(q.ask) || q.ask<q.bid) return null;
  const bidThrough=q.bidThrough??q.bidAt,askThrough=q.askThrough??q.askAt;
  const latest=Math.max(bidThrough,askThrough),oldest=Math.min(q.bidAt,q.askAt);
  if(![q.bidAt,q.askAt,bidThrough,askThrough,q.receivedAt].every(Number.isSafeInteger) ||
     bidThrough<q.bidAt || bidThrough>q.bidAt+1 || askThrough<q.askAt || askThrough>q.askAt+1 ||
     latest>q.receivedAt || q.receivedAt>now || now-oldest>MAX_DELAY) return null;
  return {symbol:'QQQ',spot:(q.bid+q.ask)/2,t:latest,receivedAt:q.receivedAt,oldest};
}

class ForwardFeed {
  reset(open,close) {
    this.open=open; this.close=close;
    this.day=new Date(open).toISOString().slice(0,10);
    this.spots=[];this.chains=[]; this.prior=null; this.pending=[]; this.lastPrices=new Map();
    this.next=null; this.lastClosed=null; this.ladder=null; this.openOi=new Map();
    this.badThrough=0; this.lastReceipt=0; this.parityAt=null; this.verified=null;
  }

  observe({flow,chain,receivedAt,chainReceivedAt,spotQuote,frozenLadderSpot,openingFrame,openingOi,open,close}) {
    if(![open,close,receivedAt].every(Number.isSafeInteger) || close<=open) throw Error('Invalid session clock');
    if(this.open!==open || this.close!==close) this.reset(open,close);
    if(receivedAt<this.lastReceipt) return {quote:null,frames:[]};
    this.lastReceipt=receivedAt;
    const ct=stamp(chain?.timestamp);
    let quote=null;
    if(chain?.symbol==='QQQ' && chain.date===this.day && chain.expiration===this.day && Array.isArray(chain.rows) &&
       positive(chain.underlying_price) && Number.isSafeInteger(chainReceivedAt) && ct<=chainReceivedAt &&
       chainReceivedAt<=receivedAt && receivedAt-ct<=OF_SNAPSHOT_MAX_AGE_MS) {
      if(!this.chains.some(x=>x.ct===ct)) this.chains.push({ct,chain,received:chainReceivedAt});
      this.chains=this.chains.filter(c=>receivedAt-c.ct<=OF_SNAPSHOT_MAX_AGE_MS+MAX_DELAY).sort((a,b)=>a.ct-b.ct);
      quote=spotObservation(chain,ct,chainReceivedAt);
    }
    for(const sample of [quote,usableBbo(spotQuote,receivedAt)]) {
      if(sample && !this.spots.some(s=>s.t===sample.t)) this.spots.push(sample);
    }
    this.spots=this.spots.filter(s=>receivedAt-(s.oldest??s.t)<=OF_SNAPSHOT_MAX_AGE_MS+MAX_DELAY).sort((a,b)=>a.t-b.t);
    const frames=[];
    if(receivedAt>close+MAX_DELAY) return {quote,frames};
    if(this.next===null && receivedAt>=open && receivedAt<=open+MAX_DELAY &&
       (flow?.session?.day!==this.day || !(flow.session.last>=open))) {
      const snap=this.chains.filter(s=>s.ct<=open && open-s.ct<=OF_SNAPSHOT_MAX_AGE_MS).at(-1);
      const observed=this.spots.filter(s=>s.t<=open && open-(s.oldest??s.t)<=OF_SNAPSHOT_MAX_AGE_MS).at(-1);
      if(snap && observed) {
        this.ladder=ladder(this.day,observed.spot);
        const source=new Map(snap.chain.rows.map(r=>[r.OptionSymbol,r.OpenInterest]));
        const contracts=this.ladder.map(c=>({...c,openOi:source.get(c.symbol)??null}));
        if(contracts.every(c=>Number.isFinite(c.openOi) && c.openOi>=0)) {
          for(const c of contracts) this.openOi.set(c.symbol,c.openOi);
          frames.push({open,close,receivedAt,eligibleSlots:[],frame:{session:this.day,t:open,
            complete:false,minuteComplete:false,openingReferenceComplete:true,oiVerified:false,
            snapshotTime:observed.t,chainTime:snap.ct,spot:observed.spot,contracts,strikes:[]}});
          this.next=open+MINUTE;this.lastClosed=open;
        }
      }
    }
    if(flow?.session?.day!==this.day) return {quote,frames};
    const session=flow.session, changes=suffix(this.prior,flow);
    if(!changes) {this.prior=null;this.badThrough=receivedAt;this.pending=[];return {quote,frames};}
    const previous=this.prior;
    const rebuilt=JSON.stringify([session.rebuilt?.at??null,session.rebuilt?.through??null,session.rebuilt?.status??null]);
    if(previous && (previous.rebuilt!==rebuilt || !sameTotals(previous.contracts,session.contracts,changes.added))) {
      changes.continuous=false;
    }
    this.prior={count:session.prints,rows:flow.rows,rebuilt,contracts:session.contracts};
    if(!changes.continuous) {
      this.pending=[];this.lastPrices.clear();
      this.badThrough=receivedAt;
    }
    // Preserve actual first receipt for the decision. A late print cannot
    // revise a committed frame or masquerade as volume in the next minute.
    for(const row of changes.added) {
      if(row.t<open) continue;
      if(this.lastClosed!==null && row.t<=this.lastClosed) {
        if(!this.ladder || this.ladder.some(c=>c.symbol===row.symbol)) this.badThrough=Math.max(this.badThrough,receivedAt);
        continue;
      }
      this.pending.push(row);
    }
    const watermark=session.last;
    if(!Number.isSafeInteger(watermark) || watermark>receivedAt || receivedAt<open) return {quote,frames};
    if(this.next===null) {
      this.next=Math.max(open,open+Math.ceil((receivedAt-MAX_DELAY-open)/MINUTE)*MINUTE);
      if(!previous && this.next>open) this.badThrough=receivedAt;
    }
    // Do not submit old decisions during a network outage or restart.
    if(this.next<receivedAt-MAX_DELAY) {
      this.next=open+Math.ceil((receivedAt-MAX_DELAY-open)/MINUTE)*MINUTE;
      this.badThrough=Math.max(this.badThrough,receivedAt);
    }
    if(this.next>watermark || this.next>receivedAt || this.next>close) return {quote,frames};
    const t=this.next;
    this.next+=MINUTE;this.lastClosed=t;
    const snap=this.chains.filter(c=>c.ct<=t && t-c.ct<=OF_SNAPSHOT_MAX_AGE_MS).at(-1);
    const sourceRows=new Map((snap?.chain.rows??[]).map(c=>[c.OptionSymbol,c]));
    const observation=this.spots.filter(s=>s.t<=t && t-(s.oldest??s.t)<=OF_SNAPSHOT_MAX_AGE_MS).at(-1);
    const spot=observation?.spot??null;
    if(!this.ladder && observation) {
      this.ladder=ladder(this.day,positive(frozenLadderSpot)?frozenLadderSpot:spot);
      const initial=openingFrame?.contracts;
      if(openingFrame?.t===open && openingFrame.session===this.day && (openingFrame.openingReferenceComplete || openingFrame.complete) &&
         Array.isArray(initial) && initial.length===16 && initial.every((c,i)=>
           c.symbol===this.ladder[i].symbol && Number.isFinite(c.openOi) && c.openOi>=0)) {
        for(const c of initial) this.openOi.set(c.symbol,c.openOi);
      }
    }
    const contracts=(this.ladder??[]).map(c=>{
      const obs=sourceRows.get(c.symbol), oc=obs?.OpenInterest;
      const oi=Number.isFinite(oc)&&oc>=0?oc:null;
      if(t===open && snap && snap.received<=receivedAt) this.openOi.set(c.symbol,oi);
      return {...c,oi,openOi:this.openOi.get(c.symbol)??null,gamma:obs?.Gamma??null,
        spot, flow:null, printCount:0,unknownCount:0,buy:0,sell:0,unknown:0,
        buyPremium:0,sellPremium:0,unknownPremium:0,price:null,time:null};
    });
    const bySymbol=new Map(contracts.map(c=>[c.symbol,c]));
    const after=[];
    let validCounts=true;
    for(const c of contracts) {
      const totals=session.contracts?.[c.symbol];
      if(!totals || !['B','S','U','prints'].every(k=>nonnegative(totals[k]))) {
        validCounts=false;continue;
      }
      c.flow={B:totals.B,S:totals.S,U:totals.U,prints:totals.prints};
    }
    for(const row of this.pending) {
      const c=bySymbol.get(row.symbol);
      if(row.t>t) {
        after.push(row);
        if(c?.flow) {c.flow[sideField[row.side]]-=row.size;c.flow.prints--;}
        continue;
      }
      if(!c) continue;
      const last=this.lastPrices.get(c.symbol);
      if(!last || row.t>last.t || row.t===last.t && row.seq>last.seq) this.lastPrices.set(c.symbol,row);
      if(row.t<=t-MINUTE) continue;
      c.printCount++;if(row.side==='unknown') c.unknownCount++;
      c[row.side]+=row.size;c[row.side+'Premium']+=row.size*row.price*100;
    }
    this.pending=after;
    for(const c of contracts) {
      const last=this.lastPrices.get(c.symbol);
      c.time=last?.t??null;c.price=last?.price??null;
      if(c.flow && Object.values(c.flow).some(n=>!nonnegative(n))) validCounts=false;
      c.cumBuy=c.flow?.B??null;c.cumSell=c.flow?.S??null;c.cumUnknown=c.flow?.U??null;
    }
    const gaps=Array.isArray(session.gaps)?session.gaps:null;
    const overlaps=(start,end)=>!gaps || gaps.some(g=>!g || (g.start==null || g.start<=end) && (g.end==null || g.end>start));
    const identity=contracts.length===16 && contracts.every(c=>Object.hasOwn(session.opening??session.contracts??{},c.symbol));
    const openingReferenceComplete=t===open && !!snap && !!observation && contracts.length===16 &&
      contracts.every(c=>Number.isFinite(c.openOi) && c.openOi>=0);
    const minuteComplete=!!observation && identity && validCounts && this.badThrough<=Math.max(open,t-MINUTE) &&
      !overlaps(Math.max(open,t-MINUTE),t);
    const sessionGood=minuteComplete && Number.isSafeInteger(session.first) && session.first<=open+MINUTE &&
      (!session.rebuilt || session.rebuilt.status==='rebuilt') && !overlaps(open,t);
    const strikes=[];
    const chainComplete=!!snap && contracts.every(c=>sourceRows.has(c.symbol));
    if(sessionGood && chainComplete) {
      for(let i=0;i<contracts.length;i+=2) strikes.push(ofStrike(contracts[i].strike,contracts.slice(i,i+2)));
    }
    const eligibleSlots=minuteComplete?[2,6,7,8,10]:[];
    const parityQualified=minuteComplete && t-(observation.oldest??observation.t)<=5000;
    if(parityQualified && this.parityAt===t-MINUTE) eligibleSlots.push(11);
    this.parityAt=parityQualified?t:null;
    if(sessionGood && chainComplete) eligibleSlots.push(1,4,5);
    const referenceValid=openingOi?.session===this.day && openingOi.open===open &&
      typeof openingOi.key==='string' && openingOi.key.length>0 && Number.isSafeInteger(openingOi.version) && openingOi.version>0 &&
      /^[a-f0-9]{64}$/.test(openingOi.sha256) && /^\d{4}-\d{2}-\d{2}$/.test(openingOi.settledSession) &&
      openingOi.settledSession<this.day && Number.isSafeInteger(openingOi.receivedAt) && openingOi.receivedAt<=receivedAt;
    const verified=referenceValid && contracts.length===16 &&
      contracts.every(c=>Number.isSafeInteger(openingOi.bySymbol?.[c.symbol]) && openingOi.bySymbol[c.symbol]>=0);
    const dated=referenceValid && openingOi.dated;
    const datedValid=!!dated && dated.session===this.day && dated.settledSession===openingOi.settledSession &&
      typeof dated.source==='string' && dated.source.length>0 && typeof dated.request==='string' &&
      /^[a-f0-9]{64}$/.test(dated.sha256) && Number.isSafeInteger(dated.receivedAt) &&
      openingOi.receivedAt<=t && dated.receivedAt<=t && dated.receivedAt<=receivedAt;
    if(datedValid) {
      for(const c of contracts) {
        const value=dated.bySymbol?.[c.symbol];
        if(Number.isSafeInteger(value) && value>=0 && value===openingOi.bySymbol[c.symbol]) c.verifiedOpenOi=value;
      }
    }
    const clearingVerified=datedValid && contracts.some(c=>Number.isSafeInteger(c.verifiedOpenOi));
    if(clearingVerified && sessionGood && this.verified?.sha256===dated.sha256 && this.verified.t===t-MINUTE)
      eligibleSlots.push(3);
    this.verified=clearingVerified && sessionGood?{sha256:dated.sha256,t}:null;
    const oiReference=referenceValid?{session:this.day,settledSession:openingOi.settledSession,key:openingOi.key,
      version:openingOi.version,sha256:openingOi.sha256,receivedAt:openingOi.receivedAt,clearingVerified}:null;
    if(clearingVerified) oiReference.dated={source:dated.source,request:dated.request,
      sha256:dated.sha256,receivedAt:dated.receivedAt,settledSession:dated.settledSession,
      ...(dated.occSha256?{archiveSha256:dated.archiveSha256,occSha256:dated.occSha256,occRequest:dated.occRequest}:{})};
    const officialOpeningReference=verified && positive(openingOi.openingSpot) &&
      Math.floor(openingOi.openingSpot)-3===contracts[0].strike ? {spot:openingOi.openingSpot,
        bySymbol:Object.fromEntries(contracts.map(c=>[c.symbol,openingOi.bySymbol[c.symbol]]))}:null;
    if(sessionGood && (officialOpeningReference || this.openOi.size===16 && contracts.every(c=>c.openOi!==null))) eligibleSlots.push(9);
    const frame={t,session:this.day,spot,snapshotTime:observation?.t??null,chainTime:snap?.ct??null,openingReferenceComplete,parityQualified,contracts,strikes,
      complete:minuteComplete,minuteComplete,oiVerified:clearingVerified,oiReference,officialOpeningReference};
    frames.push({frame,open,close,receivedAt,eligibleSlots});
    return {quote,frames};
  }
}
module.exports={ForwardFeed,FLOW_URL,CHAIN_URL};
