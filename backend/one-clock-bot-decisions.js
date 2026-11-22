import { BOT_USERNAME_RE, jsonResponse } from './common.js';
import { botUsernames } from './bots.js';
import { createStorage } from './storage.js';
import { validSession, sessionAt } from '../site/shared/one-clock-session.js';

const PAGE = 40;
const HASH = /^[a-f0-9]{64}$/;
export async function readBotDecisions(db, {bot, session, instrument, cutoff, after}) {
  const stream = 'bot-audit/' + instrument;
  const prefix = 'native:' + stream + ':' + bot + '/' + session + '/';
  // Native record primary key begins (stream,source,record_id,revision).
  // Stop inside this bot/day and seek between pages; reading a single bot
  // must not walk the floor's publication log.
  const {results} = await db.prepare(
    `SELECT d.payload, a.payload AS anchor FROM one_clock_publication_records AS d INDEXED BY sqlite_autoindex_one_clock_publication_records_1
     LEFT JOIN one_clock_publication_records a
       ON a.stream=d.stream AND a.source='bot-anchored-skew' AND a.record_id=d.record_id
       AND a.revision=d.revision AND a.received_at<=?
     WHERE d.stream=? AND d.source='bot-decision' AND d.record_id>? AND d.record_id<?
       AND d.received_at<=? AND d.session=? AND d.instrument=? AND d.revision=1
     ORDER BY d.record_id LIMIT ?`
  ).bind(cutoff,stream,prefix+(after??''),prefix+'g',cutoff,session,instrument,PAGE+1).all();
  const rows = results.slice(0,PAGE).map(entry => {
    const row=JSON.parse(entry.payload);
    if (row.value?.bot!==bot || !row.id.startsWith(prefix) || !HASH.test(row.id.slice(prefix.length))) {
      throw new Error('decision scope mismatch');
    }
    const anchor = entry.anchor ? JSON.parse(entry.anchor) : null;
    return {...row,details:{...row.details,anchor:anchor?.value??null}};
  });
  return {rows,next:results.length>PAGE?rows.at(-1).id.slice(prefix.length):null,
    coverage:{complete:false,reason:'Only retained, identified paper decisions with a publication receipt are visible.'}};
}

export async function handleBotDecisions(request,env,url) {
  if(request.method!=='GET') return jsonResponse({error:'method not allowed'},405);
  const q=url.searchParams;
  const bot=q.get('bot'), session=q.get('session'), instrument=q.get('instrument');
  const cutoff=Number(q.get('cutoff')), after=q.get('after');
  if([...q.keys()].some(k=>!['bot','session','instrument','cutoff','after'].includes(k)||q.getAll(k).length!==1)
     || !BOT_USERNAME_RE.test(bot??'') || !validSession(session) || !['QQQ','SPY','IBIT'].includes(instrument)
     || !Number.isSafeInteger(cutoff) || cutoff<1e12 || cutoff>Date.now()+60000 || sessionAt(cutoff)!==session
     || (after!==null && !HASH.test(after))) return jsonResponse({error:'invalid decision scope'},400);
  try {
    if (!(await botUsernames(env)).includes(bot)) return jsonResponse({error:'Public bot unavailable'},404);
    const account=await createStorage(env).users.get(bot);
    if(account?.is_bot!==true || account.username!==bot || String(account.strategy_id??'').startsWith('reddit_sentiment_qqq')) {
      return jsonResponse({error:'Public bot unavailable'},404);
    }
    return jsonResponse(await readBotDecisions(env.DB,{bot,session,instrument,cutoff,after}),200);
  } catch {return jsonResponse({error:'Paper decision receipts unavailable'},503);}
}
