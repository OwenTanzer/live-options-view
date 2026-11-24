'use strict';
const {createHash} = require('node:crypto');

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
  const dateLine=lines.find(line=>line.startsWith('Open interest settled '));
  if (!new RegExp('^Open interest settled '+reference.settledSession+' vs \\d{4}-\\d{2}-\\d{2}\\.$').test(dateLine))
    return null;
  const start=lines.indexOf('## Expiration '+reference.session);
  if(start<0 || !lines.slice(start+1,start+5).includes('| Strike | Type | Prior OI | Settled OI | Change |')) return null;
  const values=new Map();
  for(const line of lines.slice(start+1)) {
    if(line.startsWith('## ')) break;
    if(!/^\|\s*[0-9]/.test(line)) continue;
    const fields=line.split('|').slice(1,-1).map(x=>x.trim());
    if(fields.length!==5 || !/^\d+(\.\d{1,3})?$/.test(fields[0]) ||
       !['call','put'].includes(fields[1]) || !numericOi(fields[3])) throw Error('Malformed settled OI archive row');
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
  const header=lines.findIndex(line=>line.startsWith('ProductSymbol\t'));
  if(header<0 || !lines[header].includes('C/P\tCall\tPut\t')) throw Error('Unexpected OCC series columns');
  let firstExpiry=null;
  const products=new Map();
  for(const line of lines.slice(header+1)) {
    if(!line.trim()) continue;
    const fields=line.split(/\t+/).map(s=>s.trim());
    if(fields.length<9 || !/^\d{4}$/.test(fields[1]) || !/^\d{2}$/.test(fields[2]) ||
       !/^\d{2}$/.test(fields[3]) || !/^\d+$/.test(fields[4]) || !/^\d{3}$/.test(fields[5]))
      throw Error('Malformed OCC series row');
    const [root,year,month,day,whole,frac,rights]=fields;
    const expiry=[year,month,day].join('-');
    if(root==='QQQ' && (firstExpiry===null || expiry<firstExpiry)) firstExpiry=expiry;
    if(expiry!==reference.session) continue;
    const strike=Number(whole+'.'+frac);
    for(const [side,amount] of [['call',fields[7]],['put',fields[8]]]) {
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
