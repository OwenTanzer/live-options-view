import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { createPreview, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
const { chromium } = createRequire(import.meta.url)('playwright');
const preview = createPreview();
const browser = await chromium.launch({
  channel: 'chromium',
  ...(process.env.ARCHIVE_TEST_CHROME ? { executablePath: process.env.ARCHIVE_TEST_CHROME } : {}),
});
try {
  const page = await browser.newPage(),
    errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  let loseFirstForkResponse = true;
  const forkAttempts = [];
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
    if (r.method() === 'POST' && r.postDataJSON()?.kind === 'version') {
      forkAttempts.push(r.postDataJSON().id);
      if (loseFirstForkResponse) {
        loseFirstForkResponse = false;
        await route.abort('failed');
        return;
      }
    }
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
  await page.waitForFunction(
    () =>
      !document.querySelector('#fork').disabled &&
      document.querySelector('#message').textContent.length > 0,
  );
  await page.locator('#fork').click();
  await page.waitForFunction(() => document.querySelector('#title').textContent !== 'spy-200');
  const fork = await page.locator('#title').textContent();
  assert.deepEqual(forkAttempts, [fork, fork]);
  assert.match(await page.locator('#diff').textContent(), /lookback/);
  assert.doesNotMatch(await page.locator('#diff').textContent(), /Parameter · exposure/);
  assert.doesNotMatch(await page.locator('#facts').textContent(), /Zero trades/);
  await page.getByText('Assign or update a trial', { exact: true }).click();
  for (const [id, value] of Object.entries({
    trialId: 'lookback-trial',
    trialOwner: 'Chej',
    trialDate: new Date().toISOString().slice(0, 10),
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
  assert.ok(data.handoffId);
  assert.match(await page.locator('#facts').textContent(), /handoff/);
  const builderLink=await page.getByRole('link',{name:'Open hypothesis in Event Builder'}).getAttribute('href');
  await page.reload();
  await page.locator('#rows button').first().waitFor();
  assert.match(await page.locator('#rows').textContent(), /Chej \(assigned\)/);
  assert.equal(Object.keys(data.configuration.rule).length, 5);
  assert.match(await page.locator('#rows').textContent(), /2 versions/);
  await page.locator('#filter-entryCondition').selectOption(data.configuration.rule.entryCondition);
  assert.equal(await page.locator('#rows button').count(), 2);
  await page.locator('#filter-riskReward').selectOption('__missing__');
  assert.equal(await page.locator('#rows button').count(), 0);
  await page.locator('#reset').click();
  assert.equal(await page.locator('#rows button').count(), 2);
  assert.deepEqual(errors, []);
  await page.goto(new URL(builderLink,LOCAL_ORIGIN).href);
  await page.locator('#use-design').click();
  assert.match(await page.locator('#archive-plan').textContent(),/Fixture design/);
  await page.locator('#add-condition').click();
  await page.locator('#save-rule').click();
  const saved=JSON.parse(await page.locator('#rule-json').textContent());
  assert.deepEqual(saved.archiveHandoff,{id:data.handoffId,version:fork});
  await page.reload();
  await page.locator('#versions').selectOption(JSON.stringify([saved.id, saved.version]));
  await page.locator('#reopen').click();
  await page.locator('#rule-origin').waitFor();
  assert.match(await page.locator('#rule-origin').textContent(), new RegExp(data.handoffId));
  const designReads = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/strategy-archive') designReads.push(request.url());
  });
  await page.locator('#offline-dataset').selectOption('order-flow');
  await page.waitForURL('**/*dataset=order-flow*');
  await page.waitForFunction(() => document.querySelector('#versions')?.options.length > 0);
  assert.equal(await page.locator('#archive-design').isVisible(), false);
  assert.deepEqual(designReads, []);
  console.log('PASS: fork, diff, assignment, coverage, hypothesis download and reload');
} finally {
  await browser.close();
  preview.close();
}
