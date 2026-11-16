// trade-journal-loads.js (OA-238) — request bookkeeping for site/trade-journal.html.
// Keeps every day-list and context response tied to the date it was requested
// for and to the load generation that issued it, so a response that arrives
// after the user has switched days (or reloaded/synced) can never replace the
// current selection. Plain classic script, no DOM access: the page loads it with
// its own <script> tag, where it defines createTradeJournalLoads as a global;
// under Node it exports the same name (see the guard at the bottom) so the
// reverse-response-order regressions in tests/trade_journal_loads.test.mjs run
// the exact code the page runs.
//
// fetchJson(url) must resolve to { ok, status, body } (or reject on a network
// error). The page passes a thin wrapper around fetch().

function createTradeJournalLoads(fetchJson) {
  let generation = 0;          // bumped by every day load and every reset
  let currentDate = null;      // the date of the most recently REQUESTED day
  let loadedDate = null;       // the date whose day list is currently applied
  const contexts = new Map();  // `${date}|${position_id}` -> { gen, value }

  const key = (date, positionId) => `${date}|${positionId}`;

  // Drop every context and invalidate everything in flight.
  function reset() { generation += 1; contexts.clear(); }

  // Load one day's positions. Resolves { stale: true } when a later day load
  // (or a reset) was issued before this response arrived; the caller must then
  // apply nothing. Otherwise resolves { stale: false, date, ok, status, body }.
  async function loadDay(date) {
    reset();
    const gen = generation;
    currentDate = date;
    let res;
    try { res = await fetchJson(`/api/trade-journal?date=${encodeURIComponent(date)}`); }
    catch { res = { ok: false, status: 0, body: { error: 'network error' } }; }
    if (gen !== generation || date !== currentDate) return { stale: true, date };
    if (res.ok) loadedDate = date;
    return { stale: false, date, ...res };
  }

  // The context for a position on a date, or undefined when none was requested
  // under the current generation.
  function context(date, positionId) {
    const entry = contexts.get(key(date, positionId));
    return entry && entry.gen === generation ? entry.value : undefined;
  }

  // Request a position's context for a date unless one is already loaded or in
  // flight for that exact date and generation. Resolves true when the response
  // was applied, false when it was discarded as stale (or nothing was sent).
  async function loadContext(date, positionId) {
    const k = key(date, positionId);
    const existing = contexts.get(k);
    if (existing && existing.gen === generation) return false;
    const gen = generation;
    const entry = { gen, value: { loading: true } };
    contexts.set(k, entry);
    let value;
    try {
      const res = await fetchJson(`/api/trade-journal/context?position_id=${encodeURIComponent(positionId)}&date=${encodeURIComponent(date)}`);
      value = res.ok ? res.body : { error: (res.body && res.body.error) || `HTTP ${res.status}` };
    } catch { value = { error: 'network error' }; }
    // Applied only if no reset/day switch happened and this is still the
    // request registered for this date+position.
    if (gen !== generation || contexts.get(k) !== entry) return false;
    entry.value = value;
    return true;
  }

  return {
    loadDay, loadContext, context, reset,
    get currentDate() { return currentDate; },
    get loadedDate() { return loadedDate; },
    get generation() { return generation; },
  };
}

if (typeof module !== 'undefined') {
  module.exports = { createTradeJournalLoads };
}
