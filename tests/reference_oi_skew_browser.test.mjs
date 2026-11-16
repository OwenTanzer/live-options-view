// Render the shipped page with disposable local snapshots; never contact providers.
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import { createPreview, fixtureNetwork, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
const out = process.env.REFERENCE_OI_EVIDENCE_DIR || '/tmp/reference-oi-skew-browser';
mkdirSync(out, { recursive: true });
const preview = createPreview();
const originalFetch = globalThis.fetch;
globalThis.fetch = fixtureNetwork;
const now = new Date().toISOString(), date = now.slice(0, 10);
function snapshot(symbol) {
  const spot = symbol === 'IBIT' ? 40 : symbol === 'SPY' ? 750 : 650;
  return { symbol, timestamp: now, date, expiration: date, underlying_price: spot,
    rows: [-2, -1, 0, 1, 2].flatMap(d => ['call', 'put'].map(Type => ({
      Strike: spot + d, Type, OpenInterest: Type === 'call' ? 100 : 200,
      OptionSymbol: `${symbol}${date.replaceAll('-', '').slice(2)}${Type === 'call' ? 'C' : 'P'}${String((spot + d) * 1000).padStart(8, '0')}`,
      Expiration: date, Bid: 1, Ask: 1.2, Volume: 10,
    }))),
    market_readings: [{ reading_id: 'reference_oi_skew', symbol,
      values: { imbalance_ratio: -1/3, near_money_call_oi: 100, near_money_put_oi: 200, band_strikes: symbol === 'IBIT' ? 1 : 5 },
      status: { code: 'ok' }, calculation: { version: 'reference_oi_skew.v1', parameters: { near_money_pct: .02 } },
      input: { snapshot_timestamp: now }, freshness: { status: 'live' } }],
  };
}
for (const sym of ['QQQ', 'SPY', 'IBIT']) preview.docs[`intraday/${sym === 'QQQ' ? '' : sym.toLowerCase() + '/'}latest.json`] = JSON.stringify(snapshot(sym));
const browser = await chromium.launch({ headless: true, channel: 'chromium' });
const errors = [], requests = [];
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/*', async route => {
    const r = route.request(); requests.push(r.url());
    assert.equal(new URL(r.url()).origin, LOCAL_ORIGIN);
    const response = await preview.fetch(new Request(r.url(), { method: r.method(), headers: await r.allHeaders(), ...(r.postDataBuffer() ? { body: r.postDataBuffer() } : {}) }));
    await route.fulfill({ status: response.status, body: Buffer.from(await response.arrayBuffer()), headers: Object.fromEntries(response.headers) });
  });
  await page.goto(LOCAL_ORIGIN);
  await page.waitForFunction(() => document.querySelector('#ibit-adaptive-oi-skew-value').textContent === '−0.333');
  assert.match(await page.locator('#ibit-adaptive-oi-skew-meta').textContent(), /±5.00% window · 5 strikes \(38–42\)/);
  assert.match(await page.locator('#ibit-oi-skew-meta').textContent(), /thin: 1 strike/);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.locator('#spy-oi-skew-meter').scrollIntoViewIfNeeded();
    const dimensions = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(dimensions.scroll <= dimensions.width, JSON.stringify(dimensions));
    await page.screenshot({ path: resolve(out, `meters-${width}.png`) });
    await page.locator('#ibit-oi-skew-meter').screenshot({ path: resolve(out, `ibit-${width}.png`) });
  }
  await page.evaluate(() => referenceOiSkewMeters.IBIT.noteFailure('fixture offline'));
  assert.match(await page.locator('#ibit-adaptive-oi-skew-meta').textContent(), /stale · refresh failed/);
  await page.screenshot({ path: resolve(out, 'meters-mobile-stale.png') });
  await page.evaluate(() => referenceOiSkewMeters.IBIT.render(Date.now() + 86400000));
  assert.match(await page.locator('#ibit-adaptive-oi-skew-meta').textContent(), /session closed/);
  await page.evaluate(p => { p.rows = p.rows.filter(r => r.Strike === 40); referenceOiSkewMeters.IBIT.update(p); }, snapshot('IBIT'));
  assert.equal(await page.locator('#ibit-adaptive-oi-skew-value').textContent(), '—');
  assert.match(await page.locator('#ibit-adaptive-oi-skew-detail').textContent(), /Only 1 strike/);
  await page.screenshot({ path: resolve(out, 'meters-mobile-insufficient.png') });
  await page.evaluate(() => referenceOiSkewMeters.IBIT.update(null));
  assert.equal(await page.locator('#ibit-adaptive-oi-skew-value').textContent(), '—');
  assert.deepEqual(errors, []);
  writeFileSync(resolve(out, 'receipt.json'), JSON.stringify({ timestamp: new Date().toISOString(), widths: [1440, 390], states: ['live', 'stale/refresh failed', 'closed', 'insufficient', 'unavailable'], errors, allRequestsLocal: requests.every(x => new URL(x).origin === LOCAL_ORIGIN), requests: requests.length }, null, 2));
  console.log('Reference OI browser: desktop/mobile, live/stale/closed/insufficient/unavailable passed');
} finally { await browser.close(); preview.close(); globalThis.fetch = originalFetch; }
