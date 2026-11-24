import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';

const browser=await chromium.launch({channel:'chrome',headless:true});
try {
  const page=await browser.newPage();
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('https://candle.test/**',route=>{
    const url=new URL(route.request().url());
    if(url.pathname.startsWith('/api/'))return route.fulfill({status:401,body:'{}'});
    return route.fulfill({path:fileURLToPath(new URL('../site'+url.pathname,import.meta.url))});
  });
  await page.goto('https://candle.test/one-clock.html');
  await page.locator('#new-rule').click();
  await page.locator('#study-kind').selectOption('candle');
  await page.locator('#rule-name').fill('QQQ candle breach');
  await page.locator('#save-rule').click();
  assert.match(await page.locator('#form-status').textContent(),/Saved QQQ candle breach/);
  const saved=JSON.parse(await page.locator('#rule-json').textContent());
  await page.route('**/api/one-clock/research',route=>route.fulfill({json:{
    session:'2026-10-29',instrument:'QQQ',cutoff:Date.parse('2026-10-29T21:00:00Z'),
    rule:saved,complete:false,events:[],trials:[],coverage:{retainedVersions:1,indexComplete:false,pending:0,gaps:389}}}));
  await page.locator('#encounter-day').fill('2026-10-29');
  await page.locator('#encounter-run').click();
  await page.getByText(/Partial replay · 2026-10-29/).waitFor();
  assert.match(await page.locator('#observed-coverage').textContent(),/389 missing/);
  await page.locator('#show-builder').click();
  await page.locator('#candle-description a').click();
  for(const [day,count,trials] of [['2026-10-29',80,367],['2026-10-30',72,347],['2026-11-02',89,343]]) {
    if(await page.locator('#offline-dataset').inputValue()!==day)await page.locator('#offline-dataset').selectOption(day);
    await page.locator('#show-results').click();
    await page.locator('#result-scope').selectOption('full');
    const inspect=page.locator('#ledger').getByText('Inspect receipt');
    await inspect.first().waitFor();
    assert.equal(await inspect.count(),count);
    assert.match(await page.locator('#comparison-note').textContent(),new RegExp(trials+' eligible'));
    await inspect.first().click();
    assert.match(await page.locator('#selected-event').textContent(),/breach of/);
    await page.locator('#ledger').getByText('Replay candle close').first().click();
    assert.equal(await page.locator('#result-scope').inputValue(),'cursor');
  }
  assert.deepEqual(errors,[]);
} finally {await browser.close();}
