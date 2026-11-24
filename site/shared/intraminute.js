export const receiptMinute = at => Math.floor(at/60000)*60000;

export function model(rows,{minute,cutoff,width,strike=null}) {
  const bins=Array.from({length:60000/width},()=>({C:[0,0,0],P:[0,0,0],shares:0,prints:0}));
  const gaps=[], inspected=[], seen=new Set();
  let opened=null, signed=0, unknown=0, calls=0, puts=0;
  for(const row of rows) {
    if(row.at>cutoff) continue;
    if(row.kind==='gap') {
      if(row.reason==='stream_disconnect') opened=row.at;
      else { gaps.push([Math.min(opened??row.start,row.start),row.at]); opened=null; }
      continue;
    }
    const bin=bins[Math.floor((row.at-minute)/width)];
    if(!bin) continue;
    if(row.kind==='option' && strike!==null && row.strike!==strike) continue;
    if(row.kind==='option' || (row.kind==='stock' && !row.cancel && !row.correction)) {
      const id=[row.symbol??'QQQ',row.seq,row.cancel??false,row.correction??false].join('/');
      if(seen.has(id)) continue;
      seen.add(id);
    }
    inspected.push(row);
    if(row.kind==='quote') {
      bin.bid=row.bid; bin.ask=row.ask;
      const mid=(row.bid+row.ask)/2;
      if(bin.firstMid===undefined) bin.firstMid=mid;
      bin.mid=mid;
      bin.quoteAt=row.at;
    } else if(row.kind==='stock') {
      if(row.cancel || row.correction || row.flag!=='@') continue;
      bin.shares+=row.size; bin.prints++;
      bin.low=Math.min(bin.low??Infinity,row.price);
      bin.high=Math.max(bin.high??-Infinity,row.price);
      if (!bin.first || row.t<bin.first.t || (row.t===bin.first.t && row.seq<bin.first.seq)) bin.first=row;
      if (!bin.last || row.t>bin.last.t || (row.t===bin.last.t && row.seq>bin.last.seq)) bin.last=row;
    } else if(row.kind==='option') {
      // Direction requires a prevailing quote on a q1 or q5 classification.
      const tier=row.basis==='q1'||row.basis==='q5';
      const side=tier && row.label==='buy'?0:tier && row.label==='sell'?1:2;
      bin[row.type][side]+=row.size;
      if(side===2) unknown+=row.size; else signed+=row.size;
      if(row.type==='C') calls+=row.size; else puts+=row.size;
    }
  }
  if(opened!==null) gaps.push([opened,cutoff]);
  return {bins,gaps,inspected,signed,unknown,calls,puts};
}

// Positive values represent call buying and put selling, in signed contracts.
// A response requires observed midpoints in both bins and continuous coverage.
export function flowResponse(data, index, width, minute, cutoff) {
  const bin=data.bins[index];
  if(!bin || minute+width*(index+1)>cutoff+1)return null;
  const [cb,cs]=bin.C, [pb,ps]=bin.P, total=cb+cs+pb+ps;
  const pressure=cb-cs-pb+ps;
  const later=data.bins[index+1000/width];
  const available=minute+(index+1000/width+1)*width<=cutoff+1;
  const measured=available && bin.mid!==undefined && later?.mid!==undefined
    && !data.gaps.some(([a,b])=>a<=later.quoteAt && b>=bin.quoteAt);
  return {pressure,imbalance:total?pressure/total:null,response:measured?later.mid-bin.mid:null};
}

// Preliminary Alkalus flow classification of one owned bin.
//
// This is a FLOW classification, descriptive only — not a full option-payoff
// classification and not a trading signal. The drilldown carries no option
// premium repricing, so there is no theta, no convexity payoff and no entry
// friction to measure; it classifies only whether the bin's signed flow
// converted into midpoint displacement in its own direction, following the
// Preliminary Guideline — Alkalus taxonomy (golden time / working copper /
// lead time). Thresholds below are preliminary.
//
// Inputs, all from the owned bin only:
//   - displacement: last observed quote midpoint minus first observed quote
//     midpoint inside the bin (model() retains bin.firstMid for this);
//   - spread: the bin's last observed quote spread (ask - bid), the friction
//     unit displacement is measured in;
//   - pressure: the bin's signed contract pressure, as in flowResponse.
// The one-second-later response is deliberately NOT an input: later
// acceleration belongs to a different interval and cannot retroactively
// reclassify the owned bin. A golden second after the exit does not turn a
// correct copper exit into a mistake.
//
// Thresholds, in spread units, with signed pressure agreeing in sign with
// the displacement:
//   - golden: displacement >= 5 spreads;
//   - copper: displacement >= 1 spread and < 5 spreads;
//   - lead:   displacement < 1 spread, or the signed flow did not agree with
//     the displacement (zero or opposing pressure). Lead here means the flow
//     did not convert in its own direction; displacement against the flow is
//     non-conversion, not a separate adverse class in this preliminary scheme.
// Returns null (unclassified) when no midpoint was observed in the bin, when
// the bin holds no option prints (no flow to classify), when the spread is
// not finite and positive, or when a recorded gap overlaps the bin.
export const ALKALUS={COPPER_SPREADS:1,GOLDEN_SPREADS:5};
export function alkalusFlow(data, index, width, minute) {
  const bin=data.bins[index];
  if(!bin || bin.firstMid===undefined || bin.mid===undefined) return null;
  const [cb,cs]=bin.C, [pb,ps]=bin.P, total=cb+cs+pb+ps;
  if(!total) return null;
  const spread=bin.ask-bin.bid;
  if(!Number.isFinite(spread) || spread<=0) return null;
  const binStart=minute+index*width, binEnd=binStart+width;
  if(data.gaps.some(([a,b])=>a<binEnd && b>binStart)) return null;
  const pressure=cb-cs-pb+ps;
  const displacement=bin.mid-bin.firstMid;
  const displacementSpreads=Math.abs(displacement)/spread;
  const agrees=pressure!==0 && Math.sign(pressure)===Math.sign(displacement);
  const kind=displacementSpreads>=ALKALUS.GOLDEN_SPREADS && agrees ? 'golden'
    : displacementSpreads>=ALKALUS.COPPER_SPREADS && agrees ? 'copper' : 'lead';
  return {kind,displacement,spread,displacementSpreads,pressure,imbalance:total?pressure/total:null};
}
