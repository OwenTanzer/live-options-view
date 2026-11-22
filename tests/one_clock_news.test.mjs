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

test('truncated page chains, changed row counts and view substitution fail closed',async()=>{
  for(const corrupt of [
    (index,v)=>{v.page_count++;},
    (index,v)=>{v.rows_total++;},
    (index,v)=>{v.first_page=v.first_page.replace('/archive/','/collector/');},
    (index,v)=>{index.views.push({...v});},
  ]){
    const index=structuredClone(fixtures.index);
    const view=index.views.find(v=>v.date===base.session && v.mode==='archive' && v.stream==='all');
    corrupt(index,view);
    await assert.rejects(archiveNews({...base,cutoff:Date.parse(base.session+'T11:15:00Z')},
      async key=>key===panel.INDEX_KEY?index:fixtures.byKey(key),panel,()=>true),/archive (view|page)/);
  }
});

test('a receipt between two milliseconds first becomes visible at the later cursor',async()=>{
  const read=async key=>{
    const copy=structuredClone(fixtures.byKey(key));
    for(const row of copy.rows??[]) if(row.title.includes('Tanker') && row.revision_number===1){
      row.archived_at='2026-11-09T10:06:00.000684Z';
    }
    return copy;
  };
  const at=Date.parse(base.session+'T10:06:00Z');
  for(const [cutoff,visible] of [[at,false],[at+1,true]]){
    const {rows}=await archiveNews({...base,cutoff},read,panel,()=>true);
    assert.equal(rows.some(r=>r.value.title.includes('Tanker')),visible);
  }
});

test('unknown and prepared sources cannot be counted as recorded news',async()=>{
  for(const label of ['PREPARED research records (positioning / institutional pressure): not live collection','unknown archive']){
    await assert.rejects(archiveNews({...base,origin:'recorded',cutoff:Date.parse(base.session+'T11:15:00Z')},
      async key=>key===panel.INDEX_KEY?fixtures.index:{...fixtures.byKey(key),data_label:label},panel,()=>true),/archive provenance/);
  }
});
