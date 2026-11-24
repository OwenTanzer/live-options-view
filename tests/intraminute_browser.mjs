import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';
const browser=await chromium.launch({headless:true,channel:'chromium',executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
const origin='http://127.0.0.1:41991', sample=1794580560000;
try {
  const page=await browser.newPage({viewport:{width:900,height:800}});
  const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  let polls=0;
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());assert.equal(url.origin,origin);
    if(url.pathname==='/api/one-clock/intraminute'){
      if(url.searchParams.has('minute'))return route.fulfill({status:404,json:{}});
      polls++;const now=Date.now();
      return route.fulfill({json:{epoch:'a'.repeat(32),cursor:polls,reset:polls===1,rows:[{kind:'quote',at:now,bid:700,ask:701,bidAt:now,askAt:now}]}});
    }
    const contentType=url.pathname.endsWith('.js')?'text/javascript':url.pathname.endsWith('.css')?'text/css':url.pathname.endsWith('.json')?'application/json':'text/html';
    await route.fulfill({body:await readFile(new URL('../site'+url.pathname,import.meta.url)),contentType});
  });
  await page.goto(origin+'/intraminute.html?at='+(sample+30500));
  await page.waitForFunction(()=>document.querySelector('#state').textContent.startsWith('Archived'));
  assert.equal(await page.locator('#cursor').inputValue(),'30500');
  assert.ok(await page.locator('#rows tr').count()>0);
  await page.locator('#width').selectOption('1000');
  await page.locator('#strike').selectOption({index:1});
  await page.locator('#next').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent.includes('14:37'));
  await page.locator('#next').click();
  await page.getByText('No published archive for this minute.').waitFor();
  await page.locator('#live').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent.startsWith('Live ·'));
  await page.locator('#sample').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent.includes('14:36'));
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  assert.deepEqual(errors,[]);
  console.log('Archive, event cutoff, drilldown, adjacent minutes, coverage, live, and mobile passed');
} finally {await browser.close();}
