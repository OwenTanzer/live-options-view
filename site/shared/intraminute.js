export const receiptMinute = at => Math.floor(at/60000)*60000;

export function model(rows,{minute,cutoff,width,strike=null}) {
  const bins=Array.from({length:60000/width},()=>({C:[0,0,0],P:[0,0,0],shares:0,prints:0}));
  const gaps=[], inspected=[], seen=new Set();
  let opened=null, signed=0, unknown=0, calls=0, puts=0;
  for(const row of rows) {
    if(row.at>cutoff) continue;
    if(row.kind==='gap') {
      if(row.reason==='stream_disconnect') opened=row.at;
      else { gaps.push([row.start,row.at]); opened=null; }
      continue;
    }
    const bin=bins[Math.floor((row.at-minute)/width)];
    if(!bin) continue;
    if(row.kind==='option' && strike!==null && row.strike!==strike) continue;
    if(row.kind==='option' || row.kind==='stock') {
      const id=[row.symbol??'QQQ',row.seq,row.cancel??false,row.correction??false].join('/');
      if(seen.has(id)) continue;
      seen.add(id);
    }
    inspected.push(row);
    if(row.kind==='quote') {
      bin.bid=row.bid; bin.ask=row.ask;
      bin.mid=(row.bid+row.ask)/2;
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
