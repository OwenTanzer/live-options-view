const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const shared = require('../site/shared.js');
const html = fs.readFileSync(path.join(__dirname, '../site/index.html'), 'utf8');
const sharedHash = require('node:crypto').createHash('sha256').update(fs.readFileSync(path.join(__dirname, '../site/shared.js'))).digest('hex').slice(0, 16);
assert.ok(html.includes('shared.js?v=' + sharedHash), 'shared script URL must match its content hash on every hosting path');
// B2 (readability quest): the panel code moved out of index.html into
// site/js/squeeze-panel.js, loaded in the same position. The test loads the
// shipped module instead of slicing the page by banner text, and pins the
// ?v= hash in the page to the module's content, like shared.js above.
const panelSource = fs.readFileSync(path.join(__dirname, '../site/js/squeeze-panel.js'), 'utf8');
const panelHash = require('node:crypto').createHash('sha256').update(panelSource).digest('hex').slice(0, 16);
assert.ok(html.includes('js/squeeze-panel.js?v=' + panelHash), 'squeeze-panel script URL must match its content hash on every hosting path');
assert.ok(!html.includes('function fetchSqueezeLatest'), 'fetchSqueezeLatest must live in js/squeeze-panel.js, not inline in index.html');
const elements = {};
const el = id => elements[id] ||= { textContent: '', innerHTML: '', dataset: {}, classList: { toggle() {} } };
const calendar = require('../site/squeeze-calendar.json');
let responses = {}, calls = [];
const response = value => ({ ok: true, status: 200, json: async () => value });
const scope = { ...shared, document: { getElementById: el }, R2: '/r2-proxy', BROWSER_DATA: '/browser-data',
  escapeHtml: s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  fetch: async (url, options) => {
    calls.push([url, options]);
    const key = url.split('?')[0].split('/').at(-1);
    const value = responses[key];
    if (value instanceof Error) throw value;
    return typeof value === 'function' ? value() : value;
  },
};
const panel = new Function(...Object.keys(scope), panelSource + `
  let clock = Date.parse('2026-09-23T13:05:00Z');
  function squeezeNow() { return clock; }
  return { fetchSqueezeLatest, set now(v) { clock = Date.parse(v); },
    get pointer() { return squeezePointer; } };
`)(...Object.values(scope));
const row = {ticker:'AAA',company:'Example',price:10,scores:{factor:.6,options:.5,combined:.56},
  first_seen_at:'2026-09-23T13:00:56Z',is_new:true};
const morning = {run_id:'morning',status:'complete',sampling_mode:'scheduled',scheduled_for:'2026-09-23T13:00:00Z',
  started_at:'2026-09-23T13:00:03Z',finished_at:'2026-09-23T13:00:13Z',session_date:'2026-09-23',candidates:[row]};
const finished = {run_id:'morning',status:'finished',acquisition_status:'complete',scheduled_for:morning.scheduled_for};
const reset = () => { responses = {'latest.json':response(morning),'latest-attempt.json':response(morning),
  'latest-schedule.json':response(finished),'squeeze-calendar.json':response(calendar)}; };
(async () => {
  reset(); await panel.fetchSqueezeLatest();
  assert.equal(el('squeeze-status').dataset.state,'live');
  assert.match(el('squeeze-tbody').innerHTML,/AAA/);
  assert.match(el('squeeze-tbody').innerHTML,/squeeze-new-badge/);
  // Scores are bars on the scanner's fixed 0-1 scale, with the number kept beside them.
  assert.match(el('squeeze-tbody').innerHTML,/<span style="width:56%"><\/span><\/span><span class="squeeze-num">0\.560</);
  assert.match(el('squeeze-tbody').innerHTML,/<span style="width:60%">/);
  assert.equal(el('squeeze-warning').textContent,'');
  assert.ok(calls.filter(([u]) => !u.startsWith('/squeeze-calendar')).every(([u])=>u.includes('/v1/scheduled/')));
  assert.ok(calls.every(([,o])=>o.cache==='no-store'));
  panel.now='2026-09-23T16:12:00Z';
  for (const status of ['missed','interrupted','claimed']) {
    responses['latest-schedule.json']=response({status,scheduled_for:'2026-09-23T16:00:00Z'});
    await panel.fetchSqueezeLatest();
    assert.equal(el('squeeze-status').dataset.state,'stale');
    assert.match(el('squeeze-warning').textContent,status==='claimed'?/awaiting publication/:new RegExp(status));
    assert.match(el('squeeze-tbody').innerHTML,/AAA/);
  }
  responses['latest-schedule.json']=response({status:'finished',acquisition_status:'partial',scheduled_for:'2026-09-23T16:00:00Z'});
  responses['latest-attempt.json']=response({status:'partial',started_at:'2026-09-23T16:00:02Z'});
  await panel.fetchSqueezeLatest();assert.match(el('squeeze-warning').textContent,/partial/);
  const noon={...morning,run_id:'noon',scheduled_for:'2026-09-23T16:00:00Z',started_at:'2026-09-23T16:00:03Z',finished_at:'2026-09-23T16:00:13Z',candidates:[{...row,is_new:false}]};
  responses['latest.json']=response(noon);responses['latest-attempt.json']=response(noon);
  responses['latest-schedule.json']=response({...finished,run_id:'noon',scheduled_for:noon.scheduled_for});
  await panel.fetchSqueezeLatest();assert.equal(el('squeeze-status').dataset.state,'live');
  assert.doesNotMatch(el('squeeze-tbody').innerHTML,/squeeze-new-badge/);
  assert.equal(el('squeeze-warning').textContent,'');
  const previous=panel.pointer, previousHtml=el('squeeze-tbody').innerHTML;
  responses['latest.json']=response({...noon,run_id:'should-not-apply'});
  responses['latest-schedule.json']={ok:true,status:200,json:async()=>{throw new Error('bad JSON');}};
  await panel.fetchSqueezeLatest();assert.equal(panel.pointer,previous);assert.equal(el('squeeze-tbody').innerHTML,previousHtml);
  assert.match(el('squeeze-status').textContent,/unavailable/);
  reset();panel.now='2026-09-23T13:05:00Z';
  responses['latest.json']=response({...morning,candidates:[{...row,scores:{combined:'bad'}}]});
  await panel.fetchSqueezeLatest();assert.equal(panel.pointer,previous);
  reset();responses['latest-schedule.json']={ok:false,status:404};await panel.fetchSqueezeLatest();
  assert.equal(el('squeeze-status').dataset.state,'stale');
  reset();responses['latest.json']=response({...morning,status:'empty',candidates:[]});
  responses['latest-schedule.json']=response({...finished,acquisition_status:'empty'});
  await panel.fetchSqueezeLatest();assert.equal(el('squeeze-status').dataset.state,'live');
  assert.match(el('squeeze-tbody').innerHTML,/No eligible candidates/);
  reset();responses['latest.json']=response({...morning,candidates:[{...row,ticker:'<img src=x onerror=alert(1)>'}]});
  await panel.fetchSqueezeLatest();assert.doesNotMatch(el('squeeze-tbody').innerHTML,/<img/);
  reset();let release;
  responses['latest.json']=()=>new Promise(resolve=>{release=resolve;});
  const oldPoll=panel.fetchSqueezeLatest();reset();await panel.fetchSqueezeLatest();
  release(response({...morning,run_id:'old-poll'}));await oldPoll;assert.equal(panel.pointer.run_id,'morning');
  panel.now='2026-09-24T12:00:00Z';await panel.fetchSqueezeLatest();
  assert.equal(el('squeeze-status').dataset.state,'stale');
  assert.doesNotMatch(el('squeeze-tbody').innerHTML,/squeeze-new-badge/);
  assert.equal(el('squeeze-caption').textContent,'Earlier session — results from the 2026-09-23 scan (New York)',
    'a prior-session shortlist is captioned with its scan date and marked as earlier');
  console.log('PASS scheduled panel: atomic fetches, concurrency, statuses, rows, escaping and prior-session badges');
})().catch(e=>{console.error(e);process.exitCode=1;});
