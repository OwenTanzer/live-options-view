import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';
import {intraminute} from '../backend/one-clock-intraminute.js';
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
  assert.match(await page.locator('#response').textContent(),/Alkalus \(preliminary, flow only\) · /);
  assert.match(await page.locator('#next-second').textContent(),/Next-second observation — not part of this interval’s classification: midpoint change one second later /);
  assert.ok(await page.locator('#rows tr').count()>0);
  await page.locator('#regime-panel summary').click();
  await page.locator('#regime-source').selectOption('manual');
  await page.locator('#regime-low').fill('685.7');
  await page.locator('#regime-high').fill('686');
  await page.locator('#regime-sigma').fill('0.22');
  await page.locator('#regime-known').fill('2026-11-13T14:35');
  await page.waitForFunction(()=>document.querySelector('#regime-result').textContent.startsWith('Below '));
  const probabilities=await page.locator('#regime-result').textContent();
  assert.match(probabilities, /Inside [\d.]+% · Above [\d.]+%/);
  await page.locator('#regime-known').fill('2026-11-13T14:37');
  await page.waitForFunction(()=>document.querySelector('#regime-result').textContent.includes('known by the cursor'));
  await page.locator('#regime-panel summary').click();
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
  await page.locator('#regime-panel summary').click();
  await page.waitForFunction(()=>document.querySelector('#regime-result').textContent.includes('partial live minute'));
  await page.route('**/api/one-clock/intraminute?cursor=*',route=>route.fulfill({status:503,json:{}}));
  await page.waitForFunction(()=>document.querySelector('#regime-result').textContent.includes('Waiting for live tape to recover'));
  await page.locator('#regime-panel summary').click();
  await page.locator('#sample').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent.includes('14:36'));
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  assert.deepEqual(errors,[]);
  console.log('Archive, event cutoff, drilldown, adjacent minutes, coverage, live, and mobile passed');

  // Exercise the actual archive API handler and HTML/module entry point with
  // controlled receipts, not an evaluate() call to the classification helper.
  const owned=sample+180000;
  const q=(offset,mid)=>({kind:'quote',at:owned+offset,bid:mid-.01,ask:mid+.01,bidAt:owned+offset,askAt:owned+offset});
  const option={kind:'option',at:owned+80,t:owned+80,seq:1,symbol:'QQQ261113C00670000',type:'C',strike:670,size:10,label:'buy',basis:'q1',price:1};
  let move=.1,later=671,gaps=[],missing=false,previousCoverage='complete';
  const previous = owned - 60000;
  const previousRows = [q(-59990,668), q(-59100,669), q(-58900,670),
    {kind:'stock',at:previous+101,t:previous+101,seq:2,price:667,size:1,flag:'@'},
    {kind:'stock',at:previous+102,t:previous+102,seq:3,price:671,size:1,flag:'@'}];
  const probe=await browser.newPage({viewport:{width:1280,height:900}});
  probe.on('pageerror',e=>errors.push(String(e)));
  await probe.route('**/*',async route=>{
    const url=new URL(route.request().url());assert.equal(url.origin,origin);
    if(url.pathname==='/api/one-clock/intraminute') {
      const rows=[...(!missing?[q(10,669.08)]:[]),option,...(!missing?[q(900,669.08+move)]:[]),q(1100,later),...gaps];
      const response=await intraminute(new Request(url),{PAPER_TRADES:{get:async key=>{
        if (key === `order-flow/qqq/intraminute/v1/${previous}.json`) {
          if (previousCoverage === 'missing') return null;
          const outage = previousCoverage === 'interrupted'
            ? [{kind:'gap',at:previous+500,start:previous+100,reason:'stream_reconnect_resumed'}] : [];
          return {body:JSON.stringify({version:1,minute:previous,rows:[...previousRows,...outage]})};
        }
        assert.equal(key,`order-flow/qqq/intraminute/v1/${owned}.json`);
        return {body:JSON.stringify({version:1,minute:owned,rows})};
      }}},url);
      return route.fulfill({status:response.status,body:await response.text(),contentType:'application/json'});
    }
    const contentType=url.pathname.endsWith('.js')?'text/javascript':url.pathname.endsWith('.css')?'text/css':'text/html';
    return route.fulfill({body:await readFile(new URL('../site'+url.pathname,import.meta.url)),contentType});
  });
  async function open() {
    await probe.goto(origin+'/intraminute.html?at='+(owned+999));
    await probe.waitForFunction(()=>document.querySelector('#state').textContent.startsWith('Archived'));
    await probe.locator('#width').selectOption('1000');
    const atClose=await probe.locator('#response').textContent();
    assert.match(await probe.locator('#next-second').textContent(),/unavailable/);
    await probe.locator('#cursor').fill('1999');
    assert.equal(await probe.locator('#response').textContent(),atClose);
    return probe.locator('#response').textContent();
  }
  const first=await open();assert.match(first,/golden time/);
  const next=await probe.locator('#next-second').textContent();
  later=668;
  assert.equal(await open(),first);
  assert.notEqual(await probe.locator('#next-second').textContent(),next);
  for(const [amount,label] of [[.019999,'lead time'],[.02,'working copper'],[.099999,'working copper'],[.1,'golden time'],[-.1,'lead time']]) {
    move=amount;assert.ok((await open()).includes(label));
  }
  assert.match(await probe.locator('#response').textContent(),/against signed flow/);
  missing=true;assert.match(await open(),/unclassified - missing or invalid/);missing=false;
  gaps=[{kind:'gap',at:owned+950,start:owned+400,reason:'stream_reconnect_resumed'}];
  assert.match(await open(),/unclassified - missing or invalid/);gaps=[];
  move=.1;await open();
  if(process.env.REVIEW_SCREENSHOT) await probe.screenshot({path:process.env.REVIEW_SCREENSHOT.replace('.png','-desktop.png'),fullPage:true});
  await probe.setViewportSize({width:390,height:844});
  await probe.locator('#response').scrollIntoViewIfNeeded();
  assert.equal(await probe.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  for(const id of ['response','next-second']) {
    const box=await probe.locator('#'+id).boundingBox();assert.ok(box.x>=0 && box.x+box.width<=390);
  }
  if(process.env.REVIEW_SCREENSHOT) await probe.screenshot({path:process.env.REVIEW_SCREENSHOT,fullPage:true});
  await probe.locator('#detail').fill('1');await probe.locator('#detail').fill('0');
  assert.match(await probe.locator('#response').textContent(),/golden time/);

  await probe.locator('#regime-panel summary').click();
  const low = probe.locator('#regime-low'), high = probe.locator('#regime-high');
  const source = probe.locator('#regime-source'), result = probe.locator('#regime-result');
  await probe.waitForFunction(() => document.querySelector('#regime-low').value === '669');
  assert.equal(await source.inputValue(),'previous');
  assert.equal(await high.inputValue(),'670');
  assert.equal(await low.isEditable(),false);
  await probe.locator('#width').selectOption('100');
  assert.equal(await low.inputValue(),'668');
  await probe.locator('#trades').check();
  assert.equal(await low.inputValue(),'667');
  assert.equal(await high.inputValue(),'671');
  await probe.locator('#regime-sigma').fill('0.2');
  await probe.locator('#regime-known').fill('2026-11-13T14:38');
  assert.match(await result.textContent(),/^Below the previous candle .*Inside the previous candle .*Above the previous candle /);
  await source.selectOption('manual');
  await low.fill('669');await high.fill('670');
  assert.match(await result.textContent(),/^Below [\d.]+%/);
  await source.selectOption('previous');
  assert.equal(await low.inputValue(),'667');
  await source.selectOption('manual');
  assert.equal(await low.inputValue(),'669');
  assert.equal(await high.inputValue(),'670');
  for (const [coverage, message] of [['missing','No archived previous candle'],['interrupted','interrupted coverage']]) {
    previousCoverage = coverage;
    await open();
    await probe.locator('#regime-panel summary').click();
    await result.getByText(message,{exact:false}).waitFor();
    assert.equal(await low.inputValue(),'');
    assert.equal(await high.inputValue(),'');
    await source.selectOption('manual');
    assert.equal(await low.isEditable(),true);
  }
  assert.deepEqual(errors,[]);
  console.log('Real API/browser entry: later-only change, thresholds, adverse reason, gaps, missing input and phone evidence passed');
} finally {await browser.close();}
