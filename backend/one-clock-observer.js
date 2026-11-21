// Trusted ingestion boundary: callers supply already-authorized, allowlisted
// source observations. This module has no HTTP route, capture or trading path.
import { eventRule, evaluateEvents } from '../site/shared/one-clock-events.js';
import { instant, observation, observationKey } from '../site/shared/one-clock.js';

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).filter(k => value[k] !== undefined).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  return JSON.stringify(value) ?? 'null';
}
async function digest(value) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return [...new Uint8Array(bytes)].map(b => b.toString(16).padStart(2,'0')).join('');
}
function boundedText(value, name) {
  if (typeof value !== 'string' || !value.length || value.length > 200) throw new Error(`invalid ${name}`);
  return value;
}
const bind = (db, sql, ...args) => db.prepare(sql).bind(...args);
const list = async stmt => (await stmt.all()).results;
const scopeArgs = (namespace, s) => [namespace, s.session, s.instrument, s.origin];
const revisionKey = row => JSON.stringify([observationKey(row), row.revision]);

export async function registerObservedRule(db, { namespace, rule, registeredAt }) {
  boundedText(namespace, 'namespace');
  const validated = eventRule(rule);
  await bind(db, 'INSERT INTO one_clock_rules(namespace,rule_id,version,registered_at,payload) VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING',
    namespace, validated.id, validated.version, instant(registeredAt), canonical(validated)).run();
  return validated;
}

export async function observeBatch(db, input) {
  const { namespace, batchId, source, cursor, selection, sessionClose, collection = 'prospective' } = input;
  for (const [name,value] of Object.entries({ namespace,batchId,source,cursor })) boundedText(value,name);
  if (!['prospective','replay','backfill'].includes(collection)) throw new Error('invalid collection mode');
  const cutoff = instant(selection.cutoff);
  const close = instant(sessionClose);
  if (!Array.isArray(input.observations) || input.observations.length > 500) throw new Error('batch must contain at most 500 observations');
  const incoming = input.observations.map(observation);
  for (const row of incoming) {
    if (row.source !== source || row.session !== selection.session || row.instrument !== selection.instrument || row.origin !== selection.origin) throw new Error('observation outside authorized batch scope');
    if (row.availableAt !== null && row.availableAt > cutoff) throw new Error('observation has not arrived');
  }
  const batchDigest = await digest(canonical({ source,cursor,selection,sessionClose:close,collection,observations:incoming }));
  const priorReceipt = await bind(db,'SELECT * FROM one_clock_receipts WHERE namespace=? AND batch_id=?',namespace,batchId).first();
  if (priorReceipt) {
    if (priorReceipt.digest !== batchDigest) throw new Error('batch identity reused with different contents');
    return { duplicate: true, generation: priorReceipt.generation };
  }
  const state = await bind(db,'SELECT * FROM one_clock_state WHERE namespace=?',namespace).first();
  if (state && cutoff < state.cutoff) throw new Error('observer cannot rewind; use as-of reads');
  const stored = await list(bind(db,'SELECT * FROM one_clock_observations WHERE namespace=? AND session=? AND instrument=? AND origin=? LIMIT 10001',...scopeArgs(namespace,selection)));
  if (new Set([...stored.map(r => revisionKey(JSON.parse(r.payload))), ...incoming.map(revisionKey)]).size > 10000) throw new Error('session exceeds the 10000-record evaluation bound; partition the source view');
  const rules = await list(bind(db,'SELECT * FROM one_clock_rules WHERE namespace=? LIMIT 101',namespace));
  if (rules.length > 100) throw new Error('namespace exceeds the 100-rule evaluation bound');
  const existing = await list(bind(db,'SELECT * FROM one_clock_events WHERE namespace=? AND session=? AND instrument=? AND origin=? LIMIT 10001',...scopeArgs(namespace,selection)));
  if (existing.length > 10000) throw new Error('event scope exceeds evaluation bound');
  const history = stored.map(r => JSON.parse(r.payload));
  const receiptByRecord = new Map(stored.map(r => [revisionKey(JSON.parse(r.payload)),r]));
  for (const row of incoming) if (!receiptByRecord.has(revisionKey(row))) receiptByRecord.set(revisionKey(row),{ collection, received_at:cutoff });
  const current = new Map();
  for (const registered of rules) {
    const events = evaluateEvents({ rule:JSON.parse(registered.payload), observations:[...history,...incoming],selection,sessionClose:close });
    for (const event of events) {
      const key = await digest(event.id);
      const { matchingObservations, ...stable } = event; // Counts remain a projection, not a write per tick.
      const inputs = event.inputEvidence.map(e => receiptByRecord.get(JSON.stringify([
        JSON.stringify([e.source,event.session,event.instrument,event.origin,e.id]),e.revision])));
      const eventCollection = inputs.some(r => r?.collection === 'backfill') ? 'backfill' :
        inputs.some(r => r?.collection === 'replay') ? 'replay' :
        event.triggeredAt < registered.registered_at ? 'retrospective' : 'prospective';
      current.set(key,{ ...stable,id:key,collection:eventCollection,registeredAt:registered.registered_at });
    }
  }
  const generation = (state?.generation ?? 0) + 1;
  const statements = [bind(db,`INSERT INTO one_clock_state(namespace,generation,cutoff) VALUES(?,?,?)
    ON CONFLICT(namespace) DO UPDATE SET generation=excluded.generation,cutoff=excluded.cutoff`,namespace,generation,cutoff)];
  // The generation trigger rejects concurrent evaluations. D1 batch rolls back
  // observations, events and source checkpoint together on any failure.
  for (const row of incoming) statements.push(bind(db,`INSERT INTO one_clock_observations
    (namespace,source,record_id,revision,session,instrument,origin,received_at,collection,payload) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING`,
  namespace,row.source,row.id,row.revision,row.session,row.instrument,row.origin,cutoff,collection,canonical(row)));
  const previous = new Map(existing.map(e => [e.event_id,e]));
  for (const eventId of new Set([...previous.keys(),...current.keys()])) {
    const old = previous.get(eventId);
    const event = current.get(eventId);
    const status = event ? 'active' : 'retracted';
    const payload = event ? canonical(event) : old.payload;
    if (old && old.state === status && old.payload === payload) continue;
    const revision = (old?.revision ?? 0) + 1;
    const parsed = event ?? JSON.parse(payload);
    statements.push(bind(db,`INSERT INTO one_clock_events(namespace,event_id,session,instrument,origin,revision,payload,state)
      VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(namespace,event_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,state=excluded.state`,
    namespace,eventId,parsed.session,parsed.instrument,parsed.origin,revision,payload,status));
    statements.push(bind(db,`INSERT INTO one_clock_event_history(namespace,event_id,revision,recorded_at,batch_id,state,payload) VALUES(?,?,?,?,?,?,?)`,
    namespace,eventId,revision,cutoff,batchId,status,payload));
  }
  statements.push(bind(db,`INSERT INTO one_clock_checkpoints(namespace,source,cursor,updated_at) VALUES(?,?,?,?)
    ON CONFLICT(namespace,source) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at`,namespace,source,cursor,cutoff));
  statements.push(bind(db,'INSERT INTO one_clock_receipts(namespace,batch_id,digest,generation,committed_at) VALUES(?,?,?,?,?)',namespace,batchId,batchDigest,generation,cutoff));
  await db.batch(statements);
  return { duplicate:false,generation,events:current.size };
}

export async function observedEventsAsOf(db, { namespace, selection }) {
  boundedText(namespace,'namespace');
  const cutoff=instant(selection.cutoff);
  // recorded_at gates corrections and backfills. The source's published time
  // alone does not mean this observer had already measured or saved an outcome.
  const rows=await list(bind(db,`SELECT h.* FROM one_clock_event_history h
    JOIN one_clock_events e ON e.namespace=h.namespace AND e.event_id=h.event_id
    WHERE h.namespace=? AND e.session=? AND e.instrument=? AND e.origin=? AND h.recorded_at<=?
      AND h.revision=(SELECT MAX(h2.revision) FROM one_clock_event_history h2
        WHERE h2.namespace=h.namespace AND h2.event_id=h.event_id AND h2.recorded_at<=?)
    ORDER BY h.event_id LIMIT 10001`,...scopeArgs(namespace,selection),cutoff,cutoff));
  if(rows.length>10000) throw new Error('result exceeds scope bound');
  return rows.filter(r=>r.state==='active').map(row=>({ ...JSON.parse(row.payload),
    observerRevision:row.revision,recordedAt:row.recorded_at,correctionBatch:row.batch_id }));
}
