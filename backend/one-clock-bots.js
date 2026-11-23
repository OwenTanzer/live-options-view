// Read-only public PAPER bot ledger. Current roster membership authorizes the
// public projection; current balances, aliases and strategy assignments are not
// historical facts. R2 terminal receipt upload time is the visibility bound.
import { BOT_USERNAME_RE, jsonResponse } from './common.js';
import { botUsernames } from './bots.js';
import { createStorage, hashSessionToken } from './storage.js';
import { instant, observation } from '../site/shared/one-clock.js';
import { validSession, sessionAt } from '../site/shared/one-clock-session.js';

export const BOT_LEDGER_PAGE = 40;
const MAX_TRADES = 10000, MAX_RECEIPT = 65536;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const stamp = value => { try { return instant(value); } catch { return null; } };
function tradeInstrument(t) {
  const named=typeof t?.underlying==='string' && /^[A-Z/][A-Z0-9/._-]{0,39}$/.test(t.underlying)?t.underlying:null;
  const occ=typeof t?.sym==='string'?/^([A-Z]+)\d{6}[CP]\d{8}$/.exec(t.sym):null;
  if(occ && named && occ[1]!==named)return null;
  return named ?? occ?.[1] ?? (t?.instrument_type==='share' && typeof t.sym==='string' && /^[A-Z]{1,10}$/.test(t.sym)?t.sym:null);
}
function publicFill(t) {
  if (!t || (t.status && t.status !== 'filled') || !['buy','sell'].includes(t.side) ||
      !Number.isSafeInteger(t.qty) || t.qty <= 0 || !Number.isFinite(t.price) || t.price < 0 ||
      typeof t.sym !== 'string' || t.sym.length > 512 || stamp(t.ts) === null ||
      typeof t.underlying !== 'string' || !/^[A-Z/][A-Z0-9/._-]{0,39}$/.test(t.underlying)) return null;
  const type = t.instrument_type ?? 'option';
  if (!['option','share','futures_option','prediction'].includes(type)) return null;
  return {ts:t.ts,sym:t.sym,underlying:t.underlying,instrument_type:type,
    side:t.side,qty:t.qty,price:t.price,
    multiplier:Number.isFinite(t.multiplier) && t.multiplier>0?t.multiplier:(type==='option'?100:1),
    type:['call','put'].includes(t.type)?t.type:null,
    strike:Number.isFinite(t.strike)?t.strike:null,
    exp:typeof t.exp==='string' && /^\d{4}-\d{2}-\d{2}$/.test(t.exp)?t.exp:null};
}
export async function readBotLedgerPage(bucket, record, {bot,session,instrument,cutoff,offset=0,version=null}) {
  if (!Array.isArray(record.trades) || record.trades.length > MAX_TRADES) throw new Error('ledger bound');
  // Do not page the current-day roster's last 100 fills. Retain prior sessions
  // for opening inventory and partial reductions. Filter future events first.
  const candidates=record.trades.filter(t=>stamp(t?.ts)!==null && stamp(t.ts)<=cutoff &&
    (tradeInstrument(t)===instrument || tradeInstrument(t)===null));
  const revision=await hashSessionToken(JSON.stringify(candidates));
  if (version!==null && version!==revision) return {restart:true};
  if(offset>candidates.length) throw new Error('invalid page');
  const rows=[];let complete=!record.trades.some(t=>stamp(t?.ts)===null);
  for (const t of candidates.slice(offset,offset+BOT_LEDGER_PAGE)) {
    const value=publicFill(t), id=t.execution_request_id ?? t.execution_id;
    // Settlements/legacy entries without retained terminal receipts are gaps;
    // their event timestamp is never substituted for publication evidence.
    if (!value || value.underlying!==instrument || tradeInstrument(t)!==instrument || typeof id!=='string' || !UUID.test(id)) {complete=false;continue;}
    const object=await bucket.get(`paper-trades/requests/${id}.json`);
    if(!object || !Number.isSafeInteger(object.size) || object.size>MAX_RECEIPT || !object.etag) {complete=false;continue;}
    const availableAt=stamp(object.uploaded?.toISOString());
    if(availableAt===null || availableAt<stamp(t.ts)) {complete=false;continue;}
    // Never inspect or expose a later terminal body at an earlier cursor.
    if(availableAt>cutoff) continue;
    const text=await object.text(); if(new TextEncoder().encode(text).byteLength>MAX_RECEIPT) throw new Error('receipt bound');
    const receipt=JSON.parse(text), retained=publicFill(receipt);
    if(receipt.status!=='filled' || receipt.username!==bot || receipt.execution_request_id!==id ||
        !retained || JSON.stringify(retained)!==JSON.stringify(value)) {complete=false;continue;}
    const publicId=await hashSessionToken(`${bot}:${id}:${object.etag}`);
    rows.push(observation({source:'bot-ledger',id:publicId,revision:1,session,instrument,origin:'recorded',
      eventAt:stamp(t.ts),availableAt,evidence:`paper-receipt-sha256:${publicId}`,value,
      details:{bot,eventSession:sessionAt(stamp(t.ts)),carry:sessionAt(stamp(t.ts))<session,
        availabilityBasis:'terminal-receipt-upload',membershipBasis:'current-public-roster'}}));
  }
  const next=offset+BOT_LEDGER_PAGE<candidates.length?offset+BOT_LEDGER_PAGE:null;
  return {rows,version:revision,next,coverage:{complete,decisionCoverage:'unavailable',
    membershipBasis:'current-public-roster',availabilityBasis:'terminal-receipt-upload',
    missingReceipts:'unavailable; opening inventory cannot be certified when any page has a gap'}};
}
export async function handleOneClockBotRoster(request,env,url) {
  if(request.method!=='GET' || url.search) return jsonResponse({error:'invalid roster request'},400);
  try {
    const members=await botUsernames(env),store=createStorage(env),bots=[];
    for(const name of members) {
      const record=await store.users.get(name);
      if(!record) throw new Error('missing roster account');
      if(record.is_bot===true && record.username===name && !String(record.strategy_id??'').startsWith('reddit_sentiment_qqq')) bots.push(name);
    }
    return jsonResponse({bots:bots.sort(),membershipBasis:'current-public-roster'},200);
  } catch {return jsonResponse({error:'Public roster unavailable'},503);}
}
export async function handleOneClockBots(request,env,url) {
  if(request.method!=='GET') return jsonResponse({error:'method not allowed'},405);
  const allowed=['bot','session','instrument','cutoff','offset','version'];
  let scope;
  try {
    if([...url.searchParams.keys()].some(k=>!allowed.includes(k) || url.searchParams.getAll(k).length!==1)) throw new Error();
    scope={bot:url.searchParams.get('bot'),session:url.searchParams.get('session'),instrument:url.searchParams.get('instrument'),
      cutoff:Number(url.searchParams.get('cutoff')),offset:Number(url.searchParams.get('offset')??0),version:url.searchParams.get('version')};
    if(!BOT_USERNAME_RE.test(scope.bot??'') || !validSession(scope.session) ||
       !/^[A-Z/][A-Z0-9/._-]{0,39}$/.test(scope.instrument??'') || sessionAt(instant(scope.cutoff))!==scope.session ||
       scope.cutoff>Date.now()+60000 || !Number.isSafeInteger(scope.offset) || scope.offset<0 || scope.offset>MAX_TRADES ||
       (scope.offset>0 && scope.version===null) || (scope.version!==null && !/^[0-9a-f]{64}$/.test(scope.version))) throw new Error();
  } catch {return jsonResponse({error:'invalid ledger scope'},400);}
  try {
    const members=await botUsernames(env);
    if(!members.includes(scope.bot)) return jsonResponse({error:'public bot unavailable'},404);
    const record=await createStorage(env).users.get(scope.bot);
    if(!record?.is_bot || record.username!==scope.bot || String(record.strategy_id??'').startsWith('reddit_sentiment_qqq')) return jsonResponse({error:'public bot unavailable'},404);
    const result=await readBotLedgerPage(env.PAPER_TRADES,record,scope);
    return jsonResponse(result,result.restart?409:200);
  } catch {return jsonResponse({error:'Bot ledger unavailable or exceeded bounded read budget'},503);}
}
