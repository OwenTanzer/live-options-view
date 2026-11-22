import {selectAsOf} from '../shared/one-clock.js';
import {sessionAt, sessionInstant} from '../shared/one-clock-session.js';

// Only archive-mode revision rows can establish historical knowledge. The
// source page also contains retrospective context, which stays off this path.
export function headlinesAt(pages, selection) {
  const rows = [];
  for (const page of pages) {
    if (page.mode !== 'archive' || page.stream !== 'all') throw Error('Expected an all-stream archive view.');
    const origin = /synthetic/i.test(page.data_label) ? 'synthetic' : 'recorded';
    for (const item of page.rows) {
      if (!item.archived_at || !item.observed_at) continue;
      const archived = Date.parse(item.archived_at);
      if (!Number.isFinite(archived)) throw Error('Invalid archive timestamp.');
      if (sessionAt(archived) !== selection.session) continue;
      const availableAt = Math.max(archived, Date.parse(item.event_at));
      rows.push({source:'news-archive', session:selection.session, instrument:selection.instrument,
        id:item.item_id, revision:item.revision_number, eventAt:item.observed_at, availableAt,
        origin, evidence:item.revision_id, value:{title:item.title, outlet:item.outlet || item.source_id}});
    }
  }
  return [...selectAsOf(rows, selection)].reverse();
}

export async function archiveNews(selection, fetchJson, panel, leaseValid) {
  const index = await fetchJson(panel.INDEX_KEY);
  if (panel.validateIndex(index).length) throw Error('News archive index unavailable.');
  const start = new Date(sessionInstant(selection.session, 0)).toISOString().slice(0,10);
  const end = new Date(selection.cutoff).toISOString().slice(0,10);
  const days = [...new Set([start,end])];
  let partial = !index.complete;
  const pages = [];
  for (const date of days) {
    const view = index.views.find(v => v.date === date && v.mode === 'archive' && v.stream === 'all');
    if (!view) { partial = true; continue; }
    let key = view.first_page;
    for (let number = 1; key; number++) {
      const expected = view.first_page.replace(/page-0001\.json$/, 'page-' + String(number).padStart(4,'0') + '.json');
      if (number > panel.MAX_PAGES || key !== expected || panel.genOf(key) !== index.gen || !leaseValid()) throw Error('News archive page unavailable.');
      const page = await fetchJson(key);
      if (!leaseValid() || panel.validatePage(page).length || page.gen !== index.gen || page.date !== date ||
          page.page_number !== number || page.mode !== 'archive' || page.stream !== 'all') throw Error('Invalid news archive page.');
      pages.push(page);
      if ((/synthetic/i.test(page.data_label) ? 'synthetic' : 'recorded') !== selection.origin) partial = true;
      partial ||= !page.complete || !page.view.complete || Date.parse(page.as_of) < Math.min(selection.cutoff, Date.parse(date+'T23:59:59.999Z'));
      key = page.next_page;
    }
  }
  return {rows: headlinesAt(pages, selection), partial};
}

export function followNews({container, research, panel, fetchJson, mountLive, dashboard, events}) {
  const live = document.createElement('div');
  const replay = document.createElement('div');
  container.append(live, replay);
  let generation = 0, showing, liveMounted = false;
  let cache = new Map(), lease;
  function cached(key) {
    const current = cache;
    if (!current.has(key)) current.set(key, fetchJson(key).catch(error => {current.delete(key); throw error;}));
    return current.get(key);
  }
  async function change(selection) {
    if (showing === 'live' && selection.mode === 'live') return;
    const ticket = ++generation;
    showing = selection.mode;
    live.hidden = showing !== 'live';
    replay.hidden = showing === 'live';
    if (research) research.hidden = showing !== 'live';
    if (showing === 'live') {
      if (!liveMounted) {liveMounted = true; await mountLive(live);}
      return;
    }
    replay.textContent = 'Reading archived headlines…';
    if (!lease || !lease()) {
      cache = new Map();
      lease = panel.startLease({wall:Date.now, mono:() => performance.now()},panel.LEASE_MS);
    }
    try {
      const result = await archiveNews(selection, cached, panel, lease);
      if (ticket !== generation) return;
      const heading = document.createElement('p');
      heading.textContent = 'Market-wide archived news through '+new Date(selection.cutoff).toISOString()+
        ' · '+result.rows.length+' retained headlines'+(result.partial?' · partial archive coverage':'')+'.';
      replay.replaceChildren(heading);
      for (const row of result.rows) {
        const line = document.createElement('p');
        line.textContent = new Date(row.availableAt).toISOString()+' · '+row.value.outlet+' · '+row.value.title;
        replay.append(line);
      }
    } catch (error) {
      if (ticket === generation) replay.textContent = 'News unavailable at this cursor. '+error.message;
    }
  }
  events.addEventListener('one-clock-selection', event => change(event.detail));
  return change(dashboard.snapshot());
}
