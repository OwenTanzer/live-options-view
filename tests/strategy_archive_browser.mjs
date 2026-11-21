import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { createPreview, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
import { createStorage } from '../backend/storage.js';
const { chromium } = createRequire(import.meta.url)('playwright');
const preview = createPreview(),
  store = createStorage(preview.apiEnv);
const username = 'archive_reviewer';
await store.users.insert(username, { username, version: 0 });
await store.sessions.create('test-session', username, 3600);
const browser = await chromium.launch({
  channel: 'chromium',
  ...(process.env.ARCHIVE_TEST_CHROME ? { executablePath: process.env.ARCHIVE_TEST_CHROME } : {}),
});
try {
  const page = await browser.newPage(),
    errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.context().addCookies([{ name: 'session', value: 'test-session', url: LOCAL_ORIGIN }]);
  await page.route('**/*', async (route) => {
    const r = route.request();
    assert.equal(new URL(r.url()).origin, LOCAL_ORIGIN);
    const response = await preview.fetch(
      new Request(r.url(), {
        method: r.method(),
        headers: await r.allHeaders(),
        ...(r.postDataBuffer() ? { body: r.postDataBuffer() } : {}),
      }),
    );
    await route.fulfill({
      status: response.status,
      body: Buffer.from(await response.arrayBuffer()),
      headers: Object.fromEntries(response.headers),
    });
  });
  await page.goto(LOCAL_ORIGIN + '/strategy-archive.html');
  await page.locator('#rows button').first().click();
  await page.getByText('Fork this version', { exact: true }).click();
  const inputs = JSON.parse(await page.locator('#forkParams').inputValue());
  inputs.lookback.value = 150;
  await page.locator('#forkParams').fill(JSON.stringify(inputs));
  await page.locator('#forkReason').fill('Compare shorter lookback');
  await page.locator('#fork').click();
  await page.waitForFunction(() => document.querySelector('#title').textContent !== 'spy-200');
  const fork = await page.locator('#title').textContent();
  assert.match(await page.locator('#diff').textContent(), /lookback/);
  assert.doesNotMatch(await page.locator('#diff').textContent(), /Parameter · exposure/);
  assert.doesNotMatch(await page.locator('#facts').textContent(), /Zero trades/);
  await page.getByText('Assign or update a trial', { exact: true }).click();
  for (const [id, value] of Object.entries({
    trialId: 'lookback-trial',
    trialOwner: 'Chej',
    trialDate: '2026-11-21',
    trialSource: 'https://example.org/trials/1',
  }))
    await page.locator('#' + id).fill(value);
  await page.locator('#trial').click();
  await page.getByText('Chej (assigned)', { exact: false }).first().waitFor();
  await page.locator('#family').selectOption({ label: 'Moving-average crossover' });
  assert.match(await page.locator('#coverage').textContent(), /1 local results/);
  assert.match(await page.locator('#coverage').textContent(), /0 local results/);
  await page.getByText('Prepare hypothesis handoff').click();
  for (const input of await page.locator('#plan input').all()) await input.fill('Fixture design');
  const receipt = page.waitForEvent('download');
  await page.locator('#handoff').click();
  const data = JSON.parse(await readFile(await (await receipt).path(), 'utf8'));
  assert.equal(data.version, fork);
  assert.equal(data.configuration.parameters.lookback.value, 150);
  assert.equal(data.evidence.length, 0);
  assert.equal(data.design.Comparator, 'Fixture design');
  await page.reload();
  await page.locator('#rows button').first().waitFor();
  assert.match(await page.locator('#rows').textContent(), /Chej \(assigned\)/);
  assert.deepEqual(errors, []);
  console.log('PASS: fork, diff, assignment, coverage, hypothesis download and reload');
} finally {
  await browser.close();
  preview.close();
}
