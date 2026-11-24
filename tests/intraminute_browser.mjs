import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';
const browser=await chromium.launch({headless:true,channel:'chromium',executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
const origin='http://127.0.0.1:41991', sample=1794580560000;
try {
  const page=await browser.newPage({viewport:{width:900,height:800}});
  const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  let polls=0;
  const received=sample-172000;
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());assert.equal(url.origin,origin);
    if(url.pathname==='/api/one-clock/intraminute'){
      if(url.searchParams.has('minute'))return route.fulfill({status:404,json:{}});
      polls++;
      return route.fulfill({json:{epoch:'a'.repeat(32),cursor:polls,reset:polls===1,rows:polls===1?[{kind:'quote',at:received,bid:700,ask:701,bidAt:received,askAt:received},{kind:'gap',reason:'stream_disconnect',at:received+1,start:received+1}]:[]}});
    }
    const contentType=url.pathname.endsWith('.js')?'text/javascript':url.pathname.endsWith('.css')?'text/css':url.pathname.endsWith('.json')?'application/json':'text/html';
    await route.fulfill({body:await readFile(new URL('../site'+url.pathname,import.meta.url)),contentType});
  });
  await page.goto(origin+'/intraminute.html?at='+(sample+22699));
  await page.waitForFunction(()=>document.querySelector('#state').textContent.startsWith('Archived'));
  assert.equal(await page.locator('#cursor').inputValue(),'22699');
  assert.match(await page.locator('#response').textContent(),/At 22\.6 s · net -350 directional contracts/);
  assert.ok(await page.locator('#rows tr').count()>0);
  await page.locator('#width').selectOption('1000');
  await page.locator('#strike').selectOption({index:1});
  await page.locator('#next').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent.includes('14:37'));
  await page.locator('#next').click();
  await page.getByText('No published archive for this minute.').waitFor();
  await page.clock.setFixedTime(new Date(sample));
  await page.locator('#live').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent.startsWith('Disconnected since'));
  await page.waitForTimeout(600);
  assert.ok(polls>=2);
  assert.match(await page.locator('#state').textContent(),/last receipt 14:33:08.000 UTC · age 172.0 s/);
  await page.locator('#sample').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent.includes('14:36'));
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  assert.deepEqual(errors,[]);
  console.log('Archive, event cutoff, drilldown, adjacent minutes, coverage, live, and mobile passed');
} finally {await browser.close();}
