// Independent read-only producer. Adapters supply licensed messages; no order API.
import { createHash } from 'node:crypto';
import { compareEvent, marketKey, validateRules } from '../../backend/prediction-model.js';
export function fixed(value, scale) {
  const m = /^(\d+)(?:\.(\d+))?$/.exec(String(value));
  if (!m) throw Error('Invalid decimal');
  const digits = Math.log10(scale), fraction = (m[2] ?? '');
  if (fraction.slice(digits).replace(/0/g, '')) throw Error('Unsupported increment');
  const result = Number(m[1]) * scale + Number(fraction.slice(0, digits).padEnd(digits, '0'));
  if (!Number.isSafeInteger(result)) throw Error('Unsafe decimal');
  return result;
}
export function kalshiSnapshot(payload) {
  const book = payload.orderbook_fp;
  if (!book) throw Error('Expected documented fixed-point orderbook');
  const levels = rows => rows.map(([price, size]) => [fixed(price, 10000), fixed(size, 1)])
    .filter(x => x[1] > 0).sort((a, b) => b[0] - a[0]);
  return { yes: levels(book.yes_dollars), no: levels(book.no_dollars) };
}
export class Replay {
  constructor() { this.markets = new Map(); this.seen = new Map(); this.observations = []; this.resolutions = []; }
  apply(event) {
    const digest = JSON.stringify(event);
    if (!event.id || !Number.isFinite(Date.parse(event.receive_ts)) || !Number.isFinite(Date.parse(event.exchange_ts))) throw Error('Invalid event envelope');
    if (this.seen.has(event.id)) {
      if (this.seen.get(event.id) !== digest) throw Error('Conflicting event ID');
      return;
    }
    const key = event.market_key;
    if (event.kind === 'rules') {
      validateRules(event.rules);
      if (marketKey(event.rules) !== key) throw Error('Rule identity mismatch');
      const prior = this.markets.get(key);
      if (prior?.rule_history.some(r => r.version === event.rules.version && JSON.stringify(r) !== JSON.stringify(event.rules))) throw Error('Rule version conflict');
      this.markets.set(key, { ...prior, rules: event.rules, status: prior?.status ?? 'open',
        rule_history: [...(prior?.rule_history ?? []), event.rules], book: { ...prior?.book, synchronized: false } });
    } else {
      const m = this.markets.get(key);
      if (!m) throw Error('Rules must precede evidence');
      const b = m.book;
      if (event.kind === 'snapshot') {
        if (!Number.isSafeInteger(event.sequence) || event.sequence < 0) throw Error('Invalid sequence');
        m.book = { ...kalshiSnapshot(event.payload), sequence: event.sequence, synchronized: true, gap: false,
          exchange_ts: event.exchange_ts, receive_ts: event.receive_ts };
      } else if (event.kind === 'delta') {
        if (!b.synchronized || event.sequence !== b.sequence + 1) { b.synchronized = false; b.gap = true; }
        else {
          if (!['yes', 'no'].includes(event.side) || !Number.isSafeInteger(event.price_units) || !Number.isSafeInteger(event.size_delta)) throw Error('Invalid delta');
          const levels = new Map(b[event.side]);
          const size = (levels.get(event.price_units) ?? 0) + event.size_delta;
          if (size < 0) { b.synchronized = false; b.gap = true; }
          else {
            if (size) levels.set(event.price_units, size); else levels.delete(event.price_units);
            b[event.side] = [...levels].sort((a, z) => z[0] - a[0]);
            Object.assign(b, { sequence: event.sequence, exchange_ts: event.exchange_ts, receive_ts: event.receive_ts });
          }
        }
      } else if (event.kind === 'disconnect') { b.synchronized = false; b.gap = true; }
      else if (event.kind === 'status') {
        if (!['open', 'halted', 'closed', 'disputed', 'settled', 'delisted'].includes(event.status)) throw Error('Invalid lifecycle');
        m.status = event.status;
      } else if (event.kind === 'resolution') {
        const old = this.resolutions.find(r => r.market_key === key && r.revision === event.resolution.revision);
        if (old && ['id', 'result', 'rule_version', 'source', 'official'].some(k => old[k] !== event.resolution[k])) throw Error('Conflicting resolution revision');
        this.resolutions.push({ ...event.resolution, market_key: key, exchange_ts: event.exchange_ts, receive_ts: event.receive_ts });
        m.status = 'settled';
      } else if (event.kind === 'observation') {
        this.observations.push(compareEvent(structuredClone(m), event.options, event.spot, Date.parse(event.receive_ts)));
      } else throw Error('Unknown event kind');
    }
    if (['snapshot', 'delta', 'status', 'disconnect'].includes(event.kind)) {
      const m = this.markets.get(event.market_key);
      // Emit regardless of holdings; incompatible/missing context remains visible.
      this.observations.push(compareEvent(structuredClone(m), event.options ?? null, event.spot ?? null, Date.parse(event.receive_ts)));
    }
    this.seen.set(event.id, digest);
  }
  catalog() { return { schema: 'prediction.catalog.v1', markets: [...this.markets.values()], observations: this.observations.slice(-200) }; }
}
export function replay(events, cursor = Infinity) {
  const state = new Replay();
  // Receive order is the archive order; never sort future evidence backwards.
  for (const e of events) if (Date.parse(e.receive_ts) <= cursor && Date.parse(e.exchange_ts) <= cursor) state.apply(e);
  return state;
}
export class Collector {
  constructor({ bucket, environment = 'fixtures', enabled = false, rights, maxMarkets = 20, maxBatch = 500 }) {
    if (!enabled) throw Error('Collection disabled');
    if (!['fixtures', 'demo', 'production'].includes(environment)) throw Error('Invalid namespace');
    if (environment !== 'fixtures' && !['collection', 'retention', 'replay', 'display'].every(k => rights?.[k] === true)) throw Error('Licensed rights required');
    if (environment !== 'fixtures' && (!rights.receipt || !Number.isSafeInteger(rights.retention_days) || rights.retention_days < 1)) throw Error('Retention policy required');
    this.bucket = bucket; this.prefix = `prediction/v1/${environment}/`; this.environment = environment;
    this.maxMarkets = maxMarkets; this.maxBatch = maxBatch; this.state = new Replay(); this.pending = []; this.batches = [];
    this.health = { archive_failures: 0, writes: 0, bytes: 0, requests: 0, resnapshots: 0 };
  }
  accept(event) {
    if (this.pending.length >= this.maxBatch) throw Error('Archive backpressure: flush before collecting');
    if (event.kind === 'rules' && (event.rules.environment !== this.environment || (!this.state.markets.has(event.market_key) && this.state.markets.size >= this.maxMarkets))) throw Error('Subscription boundary');
    if (this.state.seen.has(event.id)) { this.state.apply(event); return; }
    this.state.apply(event); this.pending.push(event);

  }
  async ingest(event, resnapshot) {
    this.accept(event);
    if (this.state.markets.get(event.market_key)?.book.gap) {
      this.health.requests++; this.health.resnapshots++;
      this.accept(await resnapshot(event.market_key));
    }
  }
  async flush() {
    if (!this.pending.length) return;
    const body = JSON.stringify({ schema: 'prediction.batch.v1', events: this.pending });
    const hash = createHash('sha256').update(body).digest('hex');
    try {
      await this.bucket.put(`${this.prefix}batches/${hash}.json`, body);
      this.health.writes++; this.health.bytes += Buffer.byteLength(body);
      if (!this.batches.includes(hash)) this.batches.push(hash);
      await this.bucket.put(`${this.prefix}manifest.json`, JSON.stringify({ schema: 'prediction.manifest.v1', batches: this.batches }));
      await this.bucket.put(`${this.prefix}current.json`, JSON.stringify(this.state.catalog()));
      await this.bucket.put(`${this.prefix}resolutions.json`, JSON.stringify(this.state.resolutions));
      this.health.writes += 3; this.pending = []; return hash;
    } catch (e) { this.health.archive_failures++; throw e; }
  }
  async restore(events) {
    this.state = replay(events);
    // Recovered books are evidence, never a fresh tradable snapshot.
    for (const [key] of this.state.markets) this.accept({ id: `restart:${key}:${this.state.seen.size}`, kind: 'disconnect',
      market_key: key, exchange_ts: new Date().toISOString(), receive_ts: new Date().toISOString() });
  }
  async restoreArchive() {
    const obj = await this.bucket.get(`${this.prefix}manifest.json`);
    if (!obj) return;
    const manifest = await obj.json();
    if (manifest.schema !== 'prediction.manifest.v1') throw Error('Invalid manifest');
    const events = [];
    for (const hash of manifest.batches) {
      if (!/^[a-f0-9]{64}$/.test(hash)) throw Error('Invalid batch hash');
      const batch = await this.bucket.get(`${this.prefix}batches/${hash}.json`);
      if (!batch) throw Error('Archive gap');
      const text = await batch.text();
      if (createHash('sha256').update(text).digest('hex') !== hash) throw Error('Archive integrity failure');
      events.push(...JSON.parse(text).events);
    }
    this.batches = manifest.batches;
    await this.restore(events);
  }
  status(now = Date.now()) {
    return { ...this.health, markets: [...this.state.markets].map(([key, m]) => ({ key,
      lag_ms: now - Date.parse(m.book.exchange_ts), last_good_sequence: m.book.sequence,
      synchronized: m.book.synchronized, gap: m.book.gap })) };
  }
}
