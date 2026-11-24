import assert from 'node:assert/strict';
import { fetchLiveQuotes } from '../backend/browser-data.js';
const env = { LIVE_QUOTE_ORIGIN: 'https://existing.invalid', LIVE_QUOTE_KEY: 'old-fixture',
  PROVISIONAL_QUOTE_ORIGIN: 'https://provisional.invalid', PROVISIONAL_QUOTE_KEY: 'new-fixture' };
let calls = [];
globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response('{}'); };
for (const root of ['SLV', 'DRAM']) {
  await fetchLiveQuotes([`${root}261123C00030000`], env);
  assert.equal(calls.at(-1).url.origin, env.PROVISIONAL_QUOTE_ORIGIN);
  assert.equal(calls.at(-1).options.headers['X-Live-Quote-Key'], 'new-fixture');
  assert.equal(calls.at(-1).options.cf.cacheTtl, 0);
}
for (const root of ['QQQ', 'SPY', 'IBIT']) {
  await fetchLiveQuotes([`${root}261123C00030000`], env);
  assert.equal(calls.at(-1).url.origin, env.LIVE_QUOTE_ORIGIN);
  assert.equal(calls.at(-1).options.headers['X-Live-Quote-Key'], 'old-fixture');
}
assert.throws(() => fetchLiveQuotes(['SLV261123C00030000'], {...env, PROVISIONAL_QUOTE_KEY: ''}));
await fetchLiveQuotes(['SLV261123C00030000'], {...env, PROVISIONAL_QUOTE_ORIGIN: ''});
assert.equal(calls.at(-1).url.origin, env.LIVE_QUOTE_ORIGIN);
console.log('provisional quotes: exact new roots, separate key, unchanged old roots, fail-closed missing key');
