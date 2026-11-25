import {journalDay} from './trade-journal.js';
import {snapshotKeyTime,snapshotRow} from './trade-journal-context.js';
import {equitySession} from './one-clock-calendar.js';
import {eventRule,measureOutcome} from '../site/shared/one-clock-events.js';
import {journalEvents,journalTransition} from '../site/shared/one-clock-owen-trades.js';

const prefixes = {QQQ:'intraday/', SPY:'intraday/spy/', IBIT:'intraday/ibit/'};

// The event time is the recorded fill/alert clock. The knowledge cutoff is a
// separate argument: importing a journal today cannot create a live signal yesterday.
async function priceIndex(bucket, instrument, session, cutoff) {
  if (!Object.hasOwn(prefixes,instrument)) return [];
  if (!bucket) throw new Error('Snapshot archive unavailable');
  const day = session.replaceAll('-','');
  const prefix = prefixes[instrument]+day+'/snapshot_';
  let cursor;
  const entries = [];
  for (let page=0;page<4;page++) {
    const result = await bucket.list({prefix,limit:1000,...(cursor?{cursor}:{})});
    for (const file of result.objects) {
      const at = snapshotKeyTime(file.key,day), uploaded = file.uploaded?.getTime();
      if (at !== null && at <= cutoff && Number.isSafeInteger(uploaded) && uploaded <= cutoff)
        entries.push({file,at,uploaded});
    }
    if (!result.truncated) return entries.sort((a,b)=>a.at-b.at || a.file.key.localeCompare(b.file.key));
    cursor = result.cursor;
  }
  throw new Error('Session snapshot index exceeds replay bound');
}

export async function replayJournalFills(env,namespace,body,now) {
  const {session,ruleId,version,cutoff=now} = body;
  const bounds = equitySession(session);
  if (!bounds || !Number.isSafeInteger(cutoff) || cutoff>now || bounds.open>cutoff ||
      typeof ruleId!=='string' || !Number.isSafeInteger(version)) throw new Error('Invalid journal replay scope');
  const saved = await env.DB.prepare('SELECT payload FROM one_clock_rules WHERE namespace=? AND rule_id=? AND version=?')
    .bind(namespace,ruleId,version).first();
  if (!saved) throw new Error('Saved journal rule version required');
  const rule = eventRule(JSON.parse(saved.payload));
  if (rule.transition!==journalTransition) throw new Error('Choose an Owen Trades rule');
  const journal = await journalDay(env.DB,session,cutoff);
  const events = journalEvents(journal.positions,rule,session,cutoff), [instrument] = rule.instruments;
  if (events.length>1000) throw new Error('Journal session exceeds occurrence bound');
  const index = events.length && rule.outcomes.length ? await priceIndex(env.PAPER_TRADES,instrument,session,cutoff) : [];
  const cache = new Map();
  let bytes = 0;
  async function price(entry) {
    if (!entry) return null;
    const {file,at,uploaded} = entry;
    if (cache.has(file.key)) return cache.get(file.key);
    if (cache.size>=80 || file.size>1024*1024 || bytes+file.size>40*1024*1024)
      throw new Error('Snapshot read bound reached. Choose fewer outcome horizons.');
    const object = await env.PAPER_TRADES.get(file.key,{onlyIf:{etagMatches:file.etag}});
    if (!object?.body || object.etag!==file.etag || object.uploaded?.getTime()!==uploaded)
      throw new Error('Archived input changed during replay; retry');
    bytes += file.size;
    const spot = snapshotRow(await object.text(),null,instrument).underlying;
    const row = Number.isFinite(spot) && spot>0 ? {source:'price',id:file.key,revision:1,
      eventAt:at,availableAt:Math.max(uploaded,at),evidence:file.key+'@'+file.etag,value:{spot}} : null;
    cache.set(file.key,row);
    return row;
  }
  async function firstPrice(entries) {
    for (const entry of entries) {
      const value = await price(entry);
      if (value) return value;
    }
    return null;
  }
  for (const event of events) {
    for (const o of rule.outcomes) {
      const start = event.triggeredAt, target = start+o.horizonMs;
      const recent = index.filter(x=>x.at<=start && start-x.at<=o.maxAgeMs).reverse();
      const baseline = o.measure==='value' ? null : await firstPrice(recent);
      event.baselines[o.id] = {value:baseline?.value.spot??null,evidence:baseline};
      const endpoint = await firstPrice(index.filter(x=>x.at>=target && x.at<=Math.min(bounds.close,target+o.toleranceMs)));
      event.outcomes.push(measureOutcome(event,o,endpoint?[endpoint]:[],{cutoff},bounds.close));
    }
  }
  return {session,instrument,cutoff,rule,events,coverage:{snapshots:index.length,read:cache.size,
    reason:'Historical journal revisions and captured underlying snapshots visible at the evaluation cutoff. Fill times may be alert-time proxies; missing price evidence is left unscored.'}};
}
