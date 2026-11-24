'use strict';
const {createHash} = require('node:crypto');
const {observerSessions} = require('../../backend/one-clock-calendar.js');

const origin = 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev';
const maxBytes = 512 * 1024;
const maximumLegs = 201;

function parseReference({raw,key,version,lastModified,receivedAt,bounds,previous}) {
  if (Buffer.byteLength(raw)>maxBytes) throw Error('Opening OI packet exceeds size bound');
  const packet = JSON.parse(raw);
  const {session,open,close} = bounds;
  const [tradingDay,settledSession] = observerSessions(open);
  const prefix = 'intraday/'+session.replaceAll('-','')+'/';
  const captured = Date.parse(packet.capturedAt);
  const published = Date.parse(lastModified);
  if (tradingDay!==session || packet.schema!=='opening-oi.v1' || packet.instrument!=='QQQ' ||
      packet.session!==session || packet.settledSession!==settledSession || packet.version!==version ||
      key!==prefix+'opening-oi/v1/'+String(version).padStart(4,'0')+'.json' ||
      Date.parse(packet.openAt)!==open || !Number.isFinite(captured) || captured<open || captured>close ||
      captured>receivedAt || !Number.isFinite(published) || published>receivedAt ||
      published+999<captured || !Number.isFinite(packet.openingSpot) || packet.openingSpot<=0 ||
      typeof packet.snapshotKey!=='string' || !packet.snapshotKey.startsWith(prefix) ||
      !/^snapshot_[0-9]{12}\.csv$/.test(packet.snapshotKey.slice(prefix.length)) ||
      !Array.isArray(packet.legs) || !packet.legs.length || packet.legs.length>maximumLegs) {
    throw Error('Invalid opening OI identity, clearing session, or availability');
  }
  const bySymbol = {};
  const seen = new Set();
  const expiry = session.slice(2).replaceAll('-','');
  for (const leg of packet.legs) {
    const units = leg.strike*1000;
    if (!Number.isSafeInteger(units) || units<=0 || units>99999999 || seen.has(units)) throw Error('Invalid opening OI strike');
    seen.add(units);
    for (const [field,right] of [['call','C'],['put','P']]) {
      if (leg[field]===null) continue;
      if (!Number.isSafeInteger(leg[field]) || leg[field]<0) throw Error('Invalid opening OI quantity');
      bySymbol['QQQ'+expiry+right+String(units).padStart(8,'0')] = leg[field];
    }
  }
  const strikes = [...seen].sort((a,b)=>a-b);
  if (!Object.keys(bySymbol).length) throw Error('Opening OI packet contains no verified legs');
  if (previous && (packet.openingSpot!==previous.openingSpot || captured<previous.captured ||
      previous.strikes.join()!==strikes.join() ||
      Object.entries(previous.bySymbol).some(([symbol,oi])=>bySymbol[symbol]!==oi))) {
    throw Error('Opening OI revision changed frozen evidence');
  }
  return {session,settledSession,open,openingSpot:packet.openingSpot,receivedAt,
    version,key,sha256:createHash('sha256').update(raw).digest('hex'),captured,published,
    // The project producer records Summary.prevDayId alongside openInterest.
    // That field does not certify an OCC effective date for this OI amount.
    clearingVerified:false,bySymbol,strikes,complete:Object.keys(bySymbol).length===strikes.length*2};
}

function createOpeningOiReader(bounds,fetchImpl=fetch,clock=Date.now) {
  let previous=null;
  let retryAt=bounds.open;
  return async function read() {
    const now=clock();
    if (now<retryAt || now>bounds.close || previous?.complete) return previous;
    retryAt=now+30000;
    const version=(previous?.version??0)+1;
    const key='intraday/'+bounds.session.replaceAll('-','')+'/opening-oi/v1/'+String(version).padStart(4,'0')+'.json';
    const response=await fetchImpl(origin+'/'+key,{redirect:'error',signal:AbortSignal.timeout(4500),headers:{'Cache-Control':'no-cache'}});
    if (response.status===404) return previous;
    if (!response.ok) throw Error('Opening OI returned HTTP '+response.status);
    if (Number(response.headers.get('content-length'))>maxBytes) throw Error('Opening OI packet exceeds size bound');
    const raw=await response.text();
    previous=parseReference({raw,key,version,lastModified:response.headers.get('last-modified'),receivedAt:clock(),bounds,previous});
    retryAt=clock();
    return previous;
  };
}

module.exports = {createOpeningOiReader,parseReference};
