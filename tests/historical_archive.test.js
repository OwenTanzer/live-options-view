const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('docs/index.html', 'utf8');
const shared = fs.readFileSync('docs/shared.js', 'utf8');
const APP = 'https://live-options-view.otmooper12.workers.dev';
const chain = 'Expiration,Type,Strike,UnderlyingPrice,OpenInterest,Volume,DTE\n2026-10-27,call,500,500,200,20,0\n2026-10-27,put,500,500,100,10,0';
const prices = 'datetime,open,high,low,close\n2026-10-27T13:30:00Z,500,502,499,501';
(async () => {
  const { default: worker } = await import('../worker.js');
  const originalFetch = global.fetch;
  let routes, calls;
  global.fetch = async url => {
    assert.equal(new URL(url).origin, 'https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev');
    const key = new URL(url).pathname.slice(1);
    calls.push(key);
    const value = routes[key];
    if (value instanceof Error) throw value;
    return new Response(typeof value === 'string' ? value : '', { status: typeof value === 'number' ? value : value === undefined ? 404 : 200 });
  };
  function harness() {
    routes = { 'manifest.json': JSON.stringify({dates:['2026-10-27']}), 'raw/qqq_chain_20261027.csv':chain, 'prices/qqq_intraday_20261027.csv':prices };
    calls = [];
    const elements = {};
    const el = id => elements[id] ||= { textContent:'',innerHTML:'',style:{},classList:{add(){},remove(){}}, value:'',disabled:false };
    const ctx = vm.createContext({document:{getElementById:el}, localStorage:{getItem(){return null}}, BROWSER_DATA:'/browser-data', historyLoaded:true, console,
      fetch: url => worker.fetch(new Request(APP + url), new Proxy({}, { get(){throw Error('private binding accessed');} })) });
    vm.runInContext(shared.slice(shared.indexOf('function parseCsv('), shared.indexOf('// ── OIranges.csv')), ctx);
    vm.runInContext(html.slice(html.indexOf('// ── historical panel'),html.indexOf('// ── boot')),ctx);
    vm.runInContext(`renderHistoryHeatmap = data => { rendered = data; return true; }; renderHistWidgets = (...args) => { widgets = args; };`,ctx);
    return {ctx,el};
  }
  try {
    let {ctx,el} = harness();
    await ctx.loadHistoryManifest();
    assert.equal(el('hist-date').value,'2026-10-27');
    assert.equal(ctx.rendered.rows.length,2);
    assert.equal(ctx.widgets[3],'2026-10-27');
    assert.equal((await ctx.loadIntradayPrices('2026-10-27'))[0].c,501);
    assert.equal((await ctx.loadDailyWindow('2026-10-27')).candles.length,1);
    // Similarity uses the same cached QQQ chain and valid dates.
    vm.runInContext('buildFeatureVector = rows => rows.map(r => r.OpenInterest)',ctx);
    assert.equal((await ctx.ensureVector('2026-10-27')).length,2);
    assert.deepEqual(calls,['manifest.json','raw/qqq_chain_20261027.csv','prices/qqq_intraday_20261027.csv']);
    ({ctx,el} = harness());
    routes['raw/qqq_chain_20261027.csv'] = 404;
    routes['raw/opex/qqq_chain_20261027.csv'] = chain;
    await ctx.loadHistoryManifest();
    assert.equal(ctx.rendered.rows.length,2);
    for (const value of ['{broken','null','{}','{"dates":"bad"}','{"dates":["2026-02-30"]}','{"dates":["<img src=x>"]}',503,new Error('network')]) {
      ({ctx,el} = harness()); routes['manifest.json'] = value;
      await ctx.loadHistoryManifest();
      assert.match(el('hist-status').textContent,/Failed to load archive index/);
      assert.equal(el('hist-date').disabled,true);
      assert.equal(ctx.historyLoaded,false);
      assert.equal(ctx.rendered,undefined);
    }
    ({ctx,el} = harness()); routes['manifest.json'] = '{"dates":[]}';
    await ctx.loadHistoryManifest();
    assert.equal(el('hist-status').textContent,'Archive is empty.');
    assert.match(el('hist-no-data').textContent,/No archived dates/);
    for (const value of [404,503,new Error('offline'),'junk','Expiration,Type,Strike,UnderlyingPrice,OpenInterest\n2026-10-27,call,bad,500,1']) {
      ({ctx,el} = harness()); routes['raw/qqq_chain_20261027.csv'] = value;
      await ctx.loadHistoryManifest();
      assert.match(el('hist-status').textContent,/Error loading/);
      assert.equal(ctx.rendered,undefined);
      if(value!==404) assert.ok(!calls.some(k=>k.includes('/opex/')),'only missing daily files use legacy fallback');
    }
    ({ctx,el} = harness()); routes['raw/qqq_chain_20261027.csv'] = chain.replaceAll('2026-10-27','2026-10-30');
    await ctx.loadHistoryManifest();
    assert.match(el('hist-status').textContent,/No 0DTE chain/);
    for (const value of ['bad', prices.replace(',500,',',, '),404,503]) {
      ({ctx,el} = harness()); routes['prices/qqq_intraday_20261027.csv'] = value;
      await assert.rejects(ctx.loadIntradayPrices('2026-10-27'));
    }
    ({ctx,el} = harness());
    await assert.rejects(ctx.fetchChainCsv('../auth'));
    await assert.rejects(ctx.loadIntradayPrices('2026-02-30'));
    assert.equal(calls.length,0);
    // A slower previous selection cannot replace the selected chain/widgets.
    const pending = {};
    ctx.fetchChainCsv = date => new Promise(resolve => pending[date]=resolve);
    const older = ctx.loadHistoryDate('2026-10-26');
    const newer = ctx.loadHistoryDate('2026-10-27');
    pending['2026-10-27']([{Expiration:'2026-10-27',UnderlyingPrice:500}]); await newer;
    pending['2026-10-26']([{Expiration:'2026-10-26',UnderlyingPrice:499}]); await older;
    assert.equal(ctx.rendered.expiration,'2026-10-27');
    assert.equal(ctx.widgets[3],'2026-10-27');
    console.log('Historical: same-origin Worker integration, manifest/chain/prices, legacy fallback, malformed/missing/empty data and selection race passed');
  } finally { global.fetch = originalFetch; }
})().catch(e => {console.error(e);process.exitCode=1});
