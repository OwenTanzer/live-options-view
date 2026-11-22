import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';

const origin='https://archive.test';
const browser=await chromium.launch({channel:'chrome',headless:true});
try {
  for (const width of [1440,390]) {
    const page=await browser.newPage({viewport:{width,height:900}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',async route=>{
      const url=new URL(route.request().url());
      assert.equal(url.origin,origin);
      assert.ok(!url.pathname.startsWith('/api/'));
      return route.fulfill({path:fileURLToPath(new URL('../site'+url.pathname,import.meta.url))});
    });
    await page.goto(origin+'/one-clock.html?dataset=order-flow');
    await page.waitForFunction(()=>document.querySelector('#versions')?.options.length>0);
    await page.locator('#show-results').click();
    await page.locator('#result-scope').selectOption('full');
    const inspect=page.locator('#ledger').getByText('Inspect receipt');
    await inspect.first().waitFor();
    assert.equal(await inspect.count(),4);
    await inspect.first().click();
    const link=page.locator('#order-flow-evidence');
    assert.equal(new URL(await link.getAttribute('href'),origin).searchParams.get('one_clock_at'),'1790343060000');
    await link.click();
    await page.waitForFunction(()=>document.querySelector('#state')?.textContent.includes('Reconstructed archive'));
    assert.equal(new URL(page.url()).searchParams.get('one_clock_at'),'1790343060000');
    assert.equal(await page.locator('#play').textContent(),'Play');
    assert.deepEqual(errors,[]);
    await page.close();
  }
} finally {
  await browser.close();
}
