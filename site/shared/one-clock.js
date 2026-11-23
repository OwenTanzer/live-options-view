// One Clock: transport-independent availability selection and view lifecycle.
// Imported unchanged by browser modules, Node tests and the future observer.
// No storage, polling, network, credentials, or execution capability lives here.
const ORIGINS = new Set(['recorded', 'reconstructed', 'synthetic']);
const MODES = new Set(['live', 'replay']);

function required(value, name) {
  if (typeof value !== 'string' || !value.trim()) throw new TypeError(`${name} is required`);
  return value;
}

export function instant(value, name = 'time') {
  // Reject naive dates and epoch seconds: adapters must explicitly normalize units.
  if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 1e12) return value;
  if (typeof value === 'string' && /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    const ms = Date.parse(value);
    if (Number.isSafeInteger(ms)) return ms;
  }
  throw new TypeError(`${name} must be epoch milliseconds or an offset-qualified timestamp`);
}

function freeze(value) {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

export function observation(input) {
  const row = structuredClone(input);
  for (const key of ['source', 'id', 'instrument', 'session', 'evidence']) required(row[key], key);
  if (!ORIGINS.has(row.origin)) throw new TypeError('origin must be explicit');
  if (!Number.isSafeInteger(row.revision) || row.revision < 1) throw new TypeError('revision must be a positive integer');
  row.eventAt = instant(row.eventAt, 'eventAt');
  if (row.valueAt !== undefined) {
    row.valueAt = instant(row.valueAt, 'valueAt');
    if (row.valueAt > row.eventAt) throw new TypeError('value observation follows state change');
  }
  row.availableAt = row.availableAt == null ? null : instant(row.availableAt, 'availableAt');
  if (row.availableAt !== null && row.availableAt < row.eventAt) throw new TypeError('availability precedes observation');
  if (!Object.hasOwn(row, 'value')) throw new TypeError('value is required (null may represent a retraction)');
  // A missing publication time is never filled from a filename or fetch time.
  return freeze(row);
}

function scope(input) {
  const result = {
    session: required(input.session, 'session'),
    instrument: required(input.instrument, 'instrument'),
    cutoff: instant(input.cutoff, 'cutoff'),
    origin: input.origin ?? 'recorded',
  };
  if (!ORIGINS.has(result.origin)) throw new TypeError('invalid origin');
  return Object.freeze(result);
}

export function observationKey(row) {
  return JSON.stringify([row.source, row.session, row.instrument, row.origin, row.id]);
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  return JSON.stringify(value);
}

export function selectAsOf(inputs, selection) {
  const at = scope(selection);
  const identities = new Map();
  const seen = new Map();
  for (const input of inputs) {
    const row = observation(input);
    if (row.session !== at.session || row.instrument !== at.instrument || row.origin !== at.origin) continue;
    // Even reconstruction must declare a simulated availability time. It can
    // never silently enter the recorded cohort.
    if (row.eventAt > at.cutoff || row.availableAt === null || row.availableAt > at.cutoff) continue;
    const key = observationKey(row);
    const revisionKey = JSON.stringify([key, row.revision]);
    const encoded = canonical(row);
    if (seen.has(revisionKey) && seen.get(revisionKey) !== encoded) throw new Error('conflicting immutable observation revision');
    seen.set(revisionKey, encoded);
    const previous = identities.get(key);
    if (!previous || row.revision > previous.revision) identities.set(key, row);
  }
  return Object.freeze([...identities.values()].sort((a, b) =>
    a.eventAt - b.eventAt || a.availableAt - b.availableAt || (observationKey(a)<observationKey(b)?-1:observationKey(a)>observationKey(b)?1:0)));
}

export function readingAsOf(inputs, selection, { maxAgeMs } = {}) {
  if (!Number.isSafeInteger(maxAgeMs) || maxAgeMs < 0) throw new TypeError('maxAgeMs must be explicit');
  const at = scope(selection);
  const rows = selectAsOf(inputs, at);
  const row = rows.at(-1) ?? null;
  if (!row) return Object.freeze({ state: 'unavailable', row: null, ageMs: null, publicationAgeMs: null });
  // Retractions do not resurrect a prior value. Coverage/retraction is a source
  // fact; freshness measures the observation's age, not recent transport activity.
  const ageMs = at.cutoff - (row.valueAt ?? row.eventAt);
  const state = row.value === null ? 'unavailable' : ageMs > maxAgeMs ? 'stale' : 'fresh';
  return Object.freeze({ state, row, ageMs, publicationAgeMs: at.cutoff - row.availableAt });
}

export function createSourceRegistry() {
  const sources = new Map();
  return Object.freeze({
    register(adapter) {
      const id = required(adapter.id, 'adapter id');
      if (sources.has(id)) throw new Error(`duplicate source: ${id}`);
      required(adapter.version, 'adapter version');
      required(adapter.owner, 'adapter owner');
      if (typeof adapter.read !== 'function') throw new TypeError('adapter.read is required');
      if (!Number.isSafeInteger(adapter.maxAgeMs) || adapter.maxAgeMs < 0) throw new TypeError('adapter.maxAgeMs is required');
      // Register once. New consumers do not create their own polling loop.
      sources.set(id, Object.freeze({ ...adapter }));
    },
    get(id) {
      if (!sources.has(id)) throw new Error(`unknown source: ${id}`);
      return sources.get(id);
    },
    list() { return Object.freeze([...sources.keys()].sort()); },
  });
}

export function createClock({ registry, initial, now = Date.now }) {
  let generation = 0;
  let controller = new AbortController();
  let reads = new Map();
  let disposed = false;
  const listeners = new Set();
  let liveStops = [];
  let current;

  function normalize(input) {
    if (!MODES.has(input.mode)) throw new TypeError('mode must be live or replay');
    return Object.freeze({ ...scope({ ...input, cutoff: input.mode === 'live' ? now() : input.cutoff }), mode: input.mode });
  }
  current = normalize(initial);

  function stopLive() {
    const stops = liveStops;
    liveStops = [];
    for (const stop of stops) stop();
  }
  function snapshot() { return Object.freeze({ ...current, generation }); }
  function select(input) {
    if (disposed) throw new Error('clock disposed');
    const next = normalize(input); // Validate before disturbing the current view.
    if (next.mode !== 'live' || current.mode !== 'live' || next.session !== current.session ||
        next.instrument !== current.instrument || next.origin !== current.origin) stopLive();
    controller.abort();
    controller = new AbortController();
    reads = new Map();
    generation += 1;
    current = next;
    const state = snapshot();
    for (const listener of listeners) listener(state);
    return state;
  }
  function read(source) {
    if (disposed) throw new Error('clock disposed');
    if (reads.has(source)) return reads.get(source);
    const adapter = registry.get(source);
    const state = snapshot();
    const signal = controller.signal;
    const pending = Promise.resolve().then(() => signal.aborted ? [] : adapter.read({ ...state, signal })).then(rows => {
      if (disposed || signal.aborted || state.generation !== generation) return Object.freeze({ discarded: true });
      if (!Array.isArray(rows)) throw new TypeError('adapter.read must return observation rows');
      if (rows.some(row => row.source !== source)) throw new Error('adapter returned another source');
      const eligible = selectAsOf(rows, state);
      return Object.freeze({ discarded: false, source, adapterVersion: adapter.version,
        selection: state, rows: eligible, reading: readingAsOf(eligible, state, adapter) });
    }).catch(error => {
      if (disposed || signal.aborted || state.generation !== generation) return Object.freeze({ discarded: true });
      // Retry a failed transport, while retaining successful per-generation reuse.
      if (reads.get(source) === pending) reads.delete(source);
      throw error;
    });
    reads.set(source, pending);
    return pending;
  }
  return Object.freeze({
    snapshot, select, read,
    tick() {
      if (current.mode !== 'live') return snapshot();
      return select({ ...current, mode: 'live' });
    },
    subscribe(listener) {
      if (disposed) throw new Error('clock disposed');
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    ownLiveSubscription(stop) {
      if (typeof stop !== 'function') throw new TypeError('cleanup function required');
      if (disposed || current.mode !== 'live') { stop(); return; }
      liveStops.push(stop);
    },
    dispose() {
      disposed = true;
      controller.abort();
      stopLive();
      reads.clear();
      listeners.clear();
    },
  });
}
