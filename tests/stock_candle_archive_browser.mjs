import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';

const browser = await chromium.launch({headless:true,...(process.env.CHROME_BIN ? {executablePath:process.env.CHROME_BIN} : {})});
try {
  const page = await browser.newPage();
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const session='2026-11-23', symbol='AAPL', prefix='nasdaq/candles/AAPL';
  const receipt={schema:'snapshot_provenance.v1',dataset:'single_stock_candles',symbol,
    publication:{status:'published'},sessions:{[session]:[Date.parse(session+'T14:30Z'),Date.parse(session+'T21:00Z')]},
    files:{},history:{'1m':[]}};
  const objects=new Map();
  for (const [close,at] of [[10,'15:01'],[12,'15:03']]) {
    const body=JSON.stringify({dataset:receipt.dataset,symbol,interval:'1m',
      bars:[{t:session+'T15:00:00Z',open:10,low:9,high:12,close}]});
    const sha256=createHash('sha256').update(body).digest('hex');
    const entry={sha256,available_at:session+'T'+at+':00Z',key:prefix+'/1m.json',version_key:prefix+'/versions/'+sha256+'.json'};
    objects.set('/'+entry.version_key,body);
    receipt.history['1m'].push(entry);
    receipt.files['1m']=entry;
  }
  objects.set('/'+prefix+'/provenance.json', JSON.stringify(receipt));
  await page.route('https://pub-*.r2.dev/**',route=>{
    const body=objects.get(new URL(route.request().url()).pathname);
    return route.fulfill({status:body?200:404,contentType:'application/json',body:body??'{}'});
  });
  await page.route('https://archive.test/**',route=>{
    const path=new URL(route.request().url()).pathname;
    return path.startsWith('/api/') ? route.fulfill({status:401,body:'{}'}) :
      route.fulfill({path:fileURLToPath(new URL('../site'+path,import.meta.url))});
  });
  await page.goto('https://archive.test/one-clock.html?dataset=stock-candles&symbol=AAPL&interval=1m&session='+session);
  await page.locator('#rule-name').waitFor();
  assert.equal(await page.locator('#instrument').inputValue(),'AAPL');
  assert.equal(await page.locator('#conditions select[data-field="source"]').first().inputValue(),'candle-cache');
  // Midnight New York is 05:00 UTC. Exercise both directions on the actual shared cursor.
  for(const [seconds,expected] of [[36059,'0 eligible bars'],[36060,'last close 10'],[36180,'last close 12'],[36060,'last close 10']]) {
    await page.locator('#cursor').evaluate((el,s)=>{el.value=String(s);el.dispatchEvent(new Event('input'));},seconds);
    assert.ok((await page.locator('#replay-description').innerText()).includes(expected));
  }
  await page.locator('#show-results').click();
  await page.locator('#result-scope').selectOption('full');
  assert.ok(await page.locator('#ledger').getByText('Inspect receipt').count()>0);
  assert.deepEqual(errors,[]);
} finally {await browser.close();}
