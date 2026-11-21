import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { fixture } from './fixtures/prediction.mjs';

test('mobile table, exact rule details, paper ticket and reload retry retain server authority', async () => {
  function element() { return { value: '', textContent: '', children: [], disabled: false,
    appendChild(e) { this.children.push(e); }, replaceChildren(...e) { this.children = e; },
    after(e) { this.afterElement = e; }, showModal() { this.open = true; }, close() { this.open = false; } }; }
  const storage = new Map(), orders = [], m = fixture();
  m.rules.text = '<img src=x onerror=alert(1)> exact synthetic rule';
  let fail = true;
  const fetch = async (path, options) => {
    assert.ok(path.startsWith('/api/'), 'same-origin only');
    if (path.endsWith('/paper-trade')) {
      orders.push(JSON.parse(options.body));
      if (fail) throw Error('Connection lost after submit');
      return Response.json({ qty: 3, price: .45, execution_request_id: orders.at(-1).execution_request_id });
    }
    if (path.endsWith('/portfolio')) return Response.json({ positions: [], trades: [] });
    return Response.json({ markets: [m] });
  };
  async function load() {
    const els = Object.fromEntries(['status','search','refresh','markets','portfolio','history','detail','dismiss','title','rules','comparison','evidence','ticket','outcome','side','qty','submit','receipt'].map(k => [k, element()]));
    els.outcome.value = 'YES'; els.side.value = 'buy'; els.qty.value = '3';
    const context = { document: { getElementById: id => els[id], createElement: element }, fetch,
      sessionStorage: { getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k) },
      crypto: { randomUUID: () => '12345678-1234-4234-8234-123456789abc' } };
    vm.runInNewContext(readFileSync(new URL('../site/js/prediction.js', import.meta.url), 'utf8'), context);
    await new Promise(resolve => setTimeout(resolve, 10)); return els;
  }
  const ui = await load();
  assert.equal(ui.markets.children[0].children.length, 2);
  const row = ui.markets.children[0].children[1];
  assert.match(row.children[1].textContent, /0.4000.*0.4500/);
  row.children[4].children[0].onclick();
  assert.equal(ui.detail.open, true); assert.match(ui.rules.textContent, /<img/); // textContent, never HTML
  assert.match(ui.comparison.textContent, /unavailable/);
  await ui.ticket.onsubmit({ preventDefault() {} });
  assert.equal(orders.length, 1); assert.equal(orders[0].price, undefined); assert.ok(storage.has('prediction.pending'));
  const reloaded = await load(); fail = false;
  await reloaded.status.afterElement.onclick();
  assert.equal(orders[0].execution_request_id, orders[1].execution_request_id);
  assert.equal(storage.has('prediction.pending'), false); assert.match(reloaded.receipt.textContent, /Filled 3/);
  ui.search.value = 'unmatched'; ui.search.oninput(); assert.equal(ui.markets.children[0].children.length, 1);
  const html = readFileSync(new URL('../site/prediction.html', import.meta.url), 'utf8');
  assert.match(html, /width=device-width/); assert.match(html, /overflow-x:auto/);
});
