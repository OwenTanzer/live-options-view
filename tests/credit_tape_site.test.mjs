import assert from 'node:assert/strict';
import test from 'node:test';
import {creditTape} from '../backend/credit-tape.js';

test('private tape forwards only its own cookies to the fixed origin', async t => {
  const origin = 'https://options.moopertonic.net';
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls++;
    assert.equal(url, 'https://prolific-essence-production.up.railway.app/credit-tape/');
    assert.equal(options.headers.get('Cookie'), '__Host-cds-session=fixture');
    assert.equal(options.headers.get('Authorization'), null);
    assert.equal(options.redirect, 'manual');
    return new Response('fixture');
  });
  for (const url of ['https://other.example/credit-tape/', origin + '/credit-tape/export']) {
    assert.equal((await creditTape(new Request(url))).status, 404);
  }
  assert.equal(calls, 0);
  const response = await creditTape(new Request(origin + '/credit-tape/?unused=1', {
    headers:{Cookie:'ordinary=fixture; __Host-cds-session=fixture', Authorization:'fixture'}
  }));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'private, no-store');
  assert.equal(calls, 1);
});
