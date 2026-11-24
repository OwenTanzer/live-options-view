'use strict';
const path = require('node:path');
const { setTimeout: sleep } = require('node:timers/promises');
const { equitySession } = require('../../backend/one-clock-calendar.js');
const { sessionAt } = require('../../site/shared/one-clock-session.js');
const { createDeadlineTracker, FRAME_GRACE_MS } = require('./forward-deadlines.cjs');

const FLOW_MS = 3000;
const CHAIN_MS = 30000;
const QUOTE_MS = 10000;
const PREOPEN_MS = 120000;
const CLOSE_GRACE_MS = 60000;

function schedule(now) {
  const session = sessionAt(now);
  const bounds = equitySession(session);
  if (!bounds || now < bounds.open - PREOPEN_MS || now > bounds.close + CLOSE_GRACE_MS) return null;
  return { session, open: bounds.open, close: bounds.close };
}

// The payload remains tied to its original receipt even when an ETag is revalidated.
function artifact(url, cadence, fetchImpl, clock) {
  let next = 0, cached = null, etag = null;
  return async function read() {
    if (clock() < next) return cached;
    next = clock() + cadence;
    const response = await fetchImpl(url, {
      headers: etag ? { 'If-None-Match': etag, 'Cache-Control': 'no-cache' } : { 'Cache-Control': 'no-cache' },
      signal: AbortSignal.timeout(8000)
    });
    if (response.status === 304 && cached) return cached;
    if (!response.ok) throw Error('HTTP ' + response.status);
    const body = await response.json();
    cached = { body, receivedAt: clock() };
    etag = response.headers.get('etag');
    return cached;
  };
}

function createService({ db, paper, feeds, fetchQuote, createOpeningOiReader, fetchImpl = fetch, clock = Date.now }) {
  let current;
  const deadlines = createDeadlineTracker(db);
  async function cycle() {
    const now = clock();
    const bounds = schedule(now);
    if (!bounds) {
      current = null;
      const missedFrames = deadlines.expire(now);
      return { status: 'idle', missedFrames, decisions: missedFrames * 11 };
    }
    if (current?.session !== bounds.session) {
      paper.registerSession(db, bounds);
      deadlines.attach(bounds, now);
      current = {
        session: bounds.session, feed: new feeds.ForwardFeed(), quoteBucket: null, lastQuote: null,
        restored: paper.restoreFeedState(db, bounds.session),
        readOi: createOpeningOiReader(bounds, fetchImpl, clock), oi: null, oiPending: false, oiError: null,
        flow: artifact(feeds.FLOW_URL, FLOW_MS, fetchImpl, clock),
        chain: artifact(feeds.CHAIN_URL, CHAIN_MS, fetchImpl, clock)
      };
    }
    const errors = [];
    const state = current;
    if (state.oiError) { errors.push(state.oiError); state.oiError = null; }
    // This optional, independently rate-limited lookup cannot delay the quote or flow path.
    if (now >= bounds.open && now <= bounds.close && !state.oiPending) {
      state.oiPending = true;
      Promise.resolve().then(() => state.readOi()).then(reference => {
        if (reference) state.oi = reference;
      }).catch(error => { state.oiError = 'opening OI: ' + error.message; })
        .finally(() => { state.oiPending = false; });
    }
    async function attempt(name, fn) {
      try { return await fn(); }
      catch (error) { errors.push(name + ': ' + error.message); return null; }
    }
    // Six shared polls per minute. The :56 slot aims for a recent observation
    // just before each decision boundary, including the session open.
    const quoteBucket = Math.floor((now + 4000) / QUOTE_MS);
    const needQuote = now >= bounds.open - 20000 && current.quoteBucket !== quoteBucket &&
      (now <= bounds.close || paper.hasQuoteWork(db));
    if (needQuote) current.quoteBucket = quoteBucket;
    const [flow, chain, quote] = await Promise.all([
      attempt('flow', current.flow), attempt('chain', current.chain),
      needQuote ? attempt('quote', () => fetchQuote({ fetchImpl, now: clock })) : null
    ]);
    if (needQuote && !quote && !errors.some(message => message.startsWith('quote:'))) {
      errors.push('quote: no fresh two-sided QQQ quote');
    }
    // Record the quote before creating any new orders from this poll.
    // Slot 6 also uses this tape to establish the pre-signal breakout range.
    const quoteAccepted = quote ? paper.applyQuote(db, quote, clock()).accepted : false;
    if (quote) current.lastQuote = quote;
    const observedAt = clock();
    const observation = await attempt('frames', () => current.feed.observe({
      flow: flow?.body || null, chain: chain?.body || null,
      chainReceivedAt: chain?.receivedAt ?? null, spotQuote: current.lastQuote,
      receivedAt: observedAt, ...current.restored, openingOi: current.oi, ...bounds
    }));
    const flowSession = flow?.body?.session;
    const watermark = flowSession?.day === bounds.session && Number.isSafeInteger(flowSession.last) &&
      flowSession.last <= observedAt ? flowSession.last : null;
    // Expiration uses the instant the adapter saw the packet. A frame that
    // reaches its inclusive deadline can be committed before any later poll.
    const observed = observation?.frames ?? [];
    const missedFrames = deadlines.expire(observedAt, { session:bounds.session, flowDay:flowSession?.day, watermark });
    const relevantTime = Math.min(observedAt, bounds.close);
    if (observedAt > bounds.open + FRAME_GRACE_MS &&
        (watermark === null || relevantTime - watermark > FRAME_GRACE_MS)) {
      errors.push(flowSession?.day && flowSession.day !== bounds.session
        ? 'flow: expected '+bounds.session+' but received '+flowSession.day
        : 'flow: decision source watermark is missing or stale');
    }
    if (missedFrames) errors.push('flow: decision deadline missed');
    let accepted = 0, decisions = 0;
    for (const frame of observed) {
      const result = paper.applyFrame(db, frame, clock());
      if (result.accepted) { accepted++; decisions += result.decisions.length; }
    }
    const coverageError = deadlines.latestCoverageError(bounds.session);
    if (coverageError) errors.push('flow: ' + coverageError);
    return { status: errors.length ? 'degraded' : 'polling', session: bounds.session,
      frames: accepted, missedFrames, decisions: decisions + missedFrames * 11, quoteAccepted, errors };
  }
  return { cycle };
}

async function main(argv) {
  const once = argv.includes('--once');
  const args = argv.filter(arg => arg !== '--once');
  if (args.length !== 1 || !path.isAbsolute(args[0])) throw Error('Usage: node forward-service.cjs [--once] /absolute/state/forward-paper.sqlite');
  const paper = require('./forward-paper.cjs');
  const feeds = require('./forward-feed.cjs');
  const { fetchQqqQuote } = require('./forward-quote.cjs');
  const { createOpeningOiReader } = require('./forward-opening-oi.cjs');
  const db = paper.openPaper(args[0]);
  const service = createService({ db, paper, feeds, fetchQuote: fetchQqqQuote, createOpeningOiReader });
  const stop = new AbortController();
  const halt = () => stop.abort();
  process.on('SIGTERM', halt);
  process.on('SIGINT', halt);
  let previous, lastLog = 0;
  try {
    do {
      const start = Date.now();
      const result = await service.cycle();
      const summary = [result.status, result.session, ...(result.errors || [])].join('|');
      if (once || result.frames || result.quoteAccepted || summary !== previous || start - lastLog >= 60000) {
        console.log(JSON.stringify({ time: new Date(start).toISOString(), ...result }));
        previous = summary; lastLog = start;
      }
      if (once || stop.signal.aborted) break;
      const interval = result.status === 'idle' ? 30000 : FLOW_MS;
      await sleep(Math.max(100, interval - (Date.now() - start)), null, { signal: stop.signal });
    } while (!stop.signal.aborted);
  } catch (error) {
    if (error.name !== 'AbortError' || !stop.signal.aborted) throw error;
  } finally {
    process.off('SIGTERM', halt); process.off('SIGINT', halt);
    db.close();
  }
}

module.exports = { schedule, artifact, createService };
if (require.main === module) main(process.argv.slice(2)).catch(error => {
  console.error(error); process.exitCode = 1;
});
