import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {archiveNews} from '../site/js/one-clock-news.js';
const require = createRequire(import.meta.url);
const panel = require('../site/js/news-panel.js');
const fixtures = require('./news_panel/qualify.js').loadQualified();
const base = {origin:'synthetic', session:'2026-11-09', instrument:'QQQ'};
const select = (at, more={}) => archiveNews({...base,...more,cutoff:Date.parse('2026-11-09T'+at+'Z')},
  async key => fixtures.byKey(key),panel,()=>true);
test('One Clock admits archive revisions at the receipt boundary and restores earlier headlines on rewind', async () => {
  const before = await select('10:05:59.999');
  assert.equal(before.rows.length,0);
  const early = await select('10:06:00');
  assert.equal(early.rows.length,2);
  const first = early.rows.find(row=>row.value.title.includes('Tanker'));
  assert.equal(first.revision,1);
  assert.ok(!JSON.stringify(early.rows).includes('crew evacuated'));
  const later = await select('10:10:30');
  assert.equal(later.rows.find(row=>row.id===first.id).revision,2);
  assert.deepEqual((await select('10:06:00')).rows,early.rows);
  assert.equal((await select('11:15:00',{origin:'recorded'})).rows.length,0);
  assert.equal((await select('12:00:00')).partial,true);
});
