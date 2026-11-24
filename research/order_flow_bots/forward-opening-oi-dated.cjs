'use strict';
const {createHash} = require('node:crypto');
const {equitySession,observerSessions} = require('../../backend/one-clock-calendar.js');

const endpoint = 'https://paper-api.alpaca.markets/v2/options/contracts';
const maxBytes = 512 * 1024;
const numericOi = value => typeof value === 'string' && /^(0|[1-9][0-9]*)$/.test(value)
  && Number.isSafeInteger(Number(value));

async function readBounded(response) {
  if (Number(response.headers.get('content-length')) > maxBytes) throw Error('Dated OI exceeds size bound');
  const chunks = [];
  let bytes = 0;
  for await (const chunk of response.body) {
    bytes += chunk.length;
    if (bytes > maxBytes) throw Error('Dated OI exceeds size bound');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function parseAlpaca(raw, reference, receivedAt) {
  if (raw.length > maxBytes || !Number.isSafeInteger(receivedAt) || receivedAt < reference.receivedAt)
    throw Error('Invalid dated OI receipt');
  const response = JSON.parse(raw);
  if (!Array.isArray(response.option_contracts) || response.option_contracts.length > 1000 || response.next_page_token)
    throw Error('Incomplete contract metadata response');
  const bySymbol = {};
  const seen = new Set();
  for (const row of response.option_contracts) {
    if (!Object.hasOwn(reference.bySymbol,row?.symbol)) continue;
    if (seen.has(row.symbol)) throw Error('Duplicate dated OI contract');
    seen.add(row.symbol);
    const strike = Math.round(Number(row.strike_price)*1000);
    const right = row.type === 'call' ? 'C' : row.type === 'put' ? 'P' : '';
    if (row.underlying_symbol !== 'QQQ' || row.root_symbol !== 'QQQ' || row.size !== '100' ||
        row.expiration_date !== reference.session || !right || !Number.isInteger(strike) || strike <= 0 ||
        strike/1000 !== Number(row.strike_price) ||
        row.symbol !== 'QQQ'+reference.session.slice(2).replaceAll('-','')+right+String(strike).padStart(8,'0') ||
        row.open_interest_date !== reference.settledSession || !numericOi(row.open_interest) ||
        Number(row.open_interest) !== reference.bySymbol[row.symbol]) continue;
    bySymbol[row.symbol] = Number(row.open_interest);
  }
  return {source:'alpaca-paper-contracts',session:reference.session,settledSession:reference.settledSession,
    request:endpoint,receivedAt,sha256:createHash('sha256').update(raw).digest('hex'),bySymbol};
}

async function readAlpaca(reference, fetchImpl, clock, credentials) {
  const url = new URL(endpoint);
  url.search = new URLSearchParams({underlying_symbols:'QQQ', expiration_date:reference.session,
    strike_price_gte:String(reference.strikes[0]/1000), strike_price_lte:String(reference.strikes.at(-1)/1000),limit:'1000'});
  const response = await fetchImpl(url,{method:'GET',redirect:'error',signal:AbortSignal.timeout(4500),
    headers:{'APCA-API-KEY-ID':credentials.id,'APCA-API-SECRET-KEY':credentials.secret}});
  if (!response.ok) throw Error('Dated OI returned HTTP '+response.status);
  const raw = await readBounded(response);
  const dated = parseAlpaca(raw,reference,clock());
  dated.request = url.toString();
  return dated;
}

module.exports = {parseAlpaca,readAlpaca};

const archiveUrl = 'https://squawkflow.com/oi-changes/QQQ.md';
const occUrl = 'https://marketdata.theocc.com/series-search?symbolType=U&symbol=QQQ';
function archiveRows(raw, reference) {
  const lines=raw.toString().split(/\r?\n/);
  const dateLines=lines.filter(line=>line.startsWith('Open interest settled '));
  const dates=dateLines.length===1 && /^Open interest settled (\d{4}-\d{2}-\d{2}) vs (\d{4}-\d{2}-\d{2})\.$/.exec(dateLines[0]);
  if(!dates || dates[1]!==reference.settledSession || dates[2]>=dates[1]) return null;
  const [session,settled]=observerSessions(reference.open);
  if(reference.session!==session || dates[1]!==settled || !equitySession(dates[2])) return null;
  const monthDay=new Intl.DateTimeFormat('en-US',{month:'short',day:'numeric',timeZone:'UTC'})
    .format(new Date(settled+'T12:00:00Z'));
  if(lines[0]!=='# QQQ Options Open Interest by Strike, '+monthDay+' Settlement' ||
     lines.filter(line=>line.startsWith('# ')).length!==1) return null;
  const computed=lines.filter(line=>line.startsWith('Computed from the '));
  if(computed.length!==1 || !computed[0].startsWith('Computed from the '+monthDay+' settlement.')) return null;
  const label='## Expiration '+reference.session;
  const start=lines.indexOf(label);
  if(start<0 || start!==lines.lastIndexOf(label)) return null;
  const table=lines.slice(start+1);
  while(table[0]==='') table.shift();
  if(table.shift()!=='| Strike | Type | Prior OI | Settled OI | Change |' ||
     table.shift()!=='|---:|---|---:|---:|---:|') return null;
  const values=new Map();
  for(const line of table) {
    if(line.startsWith('## ')) break;
    if(!line.startsWith('|')) continue;
    if (!line.endsWith('|')) throw Error('Truncated settled OI archive row');
    const fields=line.split('|').slice(1,-1).map(x=>x.trim());
    if(fields.length!==5 || !/^\d+(\.\d{1,3})?$/.test(fields[0]) ||
       !['call','put'].includes(fields[1]) || !numericOi(fields[2]) || !numericOi(fields[3]) ||
       !/^[+-]?\d+$/.test(fields[4]) || Number(fields[3])-Number(fields[2])!==Number(fields[4])) throw Error('Malformed settled OI archive row');
    const key=Number(fields[0])+'/'+fields[1];
    if(values.has(key)) throw Error('Duplicate settled OI archive row');
    values.set(key,Number(fields[3]));
  }
  return values;
}

function matchPublicSeries(archive, occ, reference, receivedAt) {
  const values=archiveRows(archive,reference);
  if(!values || !Number.isSafeInteger(receivedAt) || receivedAt<reference.receivedAt) return null;
  const lines=occ.toString().split(/\r?\n/);
  if(lines[0]!=='Series Search Results for QQQ') throw Error('Unexpected OCC series identity');
  const columns='ProductSymbol\tyear\tMonth\tDay\tInteger\tDec\tC/P\tCall\tPut\tPosition Limit';
  const headers=lines.filter(line=>line.startsWith('ProductSymbol'));
  if(headers.length!==1 || headers[0].replace(/\t$/,'')!==columns)
    throw Error('Unexpected OCC series columns');
  const header=lines.indexOf(headers[0]);
  let firstExpiry=null;
  const products=new Map();
  for(const line of lines.slice(header+1)) {
    if(!line.trim()) continue;
    // The data layout has one padding column after the product symbol. Empty
    // OI cells occupy their own column. Merging tabs can turn a put into a call.
    const fields=line.split('\t').map(s=>s.trim());
    if(fields.length!==11 || !fields[0] || fields[1]!=='' || !/^\d{4}$/.test(fields[2]) ||
       !/^\d{2}$/.test(fields[3]) || !/^\d{2}$/.test(fields[4]) ||
       !/^\d+$/.test(fields[5]) || !/^\d{3}$/.test(fields[6]) ||
       !['C','P','C P'].includes(fields[7]) || !numericOi(fields[10]) ||
       [fields[8],fields[9]].some(value=>value!=='' && !numericOi(value)))
      throw Error('Malformed OCC series row');
    const [root,,year,month,day,whole,frac,rights,call,put]=fields;
    const expiry=[year,month,day].join('-');
    const instant=Date.parse(expiry+'T00:00:00Z');
    if (!Number.isFinite(instant) || new Date(instant).toISOString().slice(0,10)!==expiry)
      throw Error('Invalid OCC expiration');
    if(root==='QQQ' && (firstExpiry===null || expiry<firstExpiry)) firstExpiry=expiry;
    if(expiry!==reference.session) continue;
    const strike=Number(whole+'.'+frac);
    for(const [side,amount] of [['call',call],['put',put]]) {
      if(!rights.split(/\s+/).includes(side==='call'?'C':'P')) continue;
      const key=strike+'/'+side;
      if(products.has(key)) products.set(key,null); // any second root makes the archive key ambiguous
      else products.set(key,{root,amount});
    }
  }
  if(firstExpiry!==reference.session) return null;
  const bySymbol={};
  const prefix='QQQ'+reference.session.slice(2).replaceAll('-','');
  for(const [key,value] of values) {
    const [strike,side]=key.split('/');
    const product=products.get(key),symbol=prefix+(side==='call'?'C':'P')+String(Math.round(Number(strike)*1000)).padStart(8,'0');
    if(product?.root==='QQQ' && numericOi(product.amount) && Number(product.amount)===value &&
       reference.bySymbol[symbol]===value) bySymbol[symbol]=value;
  }
  const archiveSha256=createHash('sha256').update(archive).digest('hex');
  const occSha256=createHash('sha256').update(occ).digest('hex');
  return {source:'dated-archive-and-occ-series',session:reference.session,settledSession:reference.settledSession,
    request:archiveUrl,occRequest:occUrl,archiveSha256,occSha256,receivedAt,
    sha256:createHash('sha256').update(archiveSha256+occSha256).digest('hex'),bySymbol};
}

async function readPublic(reference,fetchImpl,clock) {
  async function body(url) {
    const response=await fetchImpl(url,{redirect:'error',signal:AbortSignal.timeout(4500)});
    if(!response.ok) throw Error('Public dated OI returned HTTP '+response.status);
    return readBounded(response);
  }
  const archive=await body(archiveUrl);
  if(!archiveRows(archive,reference)) return null; // a stale settlement needs no OCC fetch
  const occ=await body(occUrl);
  return matchPublicSeries(archive,occ,reference,clock());
}

module.exports.readPublic=readPublic;
module.exports.matchPublicSeries=matchPublicSeries;
