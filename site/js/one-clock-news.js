import {selectAsOf} from '../shared/one-clock.js';
import {sessionAt, sessionInstant} from '../shared/one-clock-session.js';

// Only archive-mode revision rows can establish historical knowledge. The
// source page also contains retrospective context, which stays off this path.
function availableMillisecond(iso) {
  const floor = Date.parse(iso);
  if (!Number.isFinite(floor)) throw Error('Invalid news timestamp.');
  // Python publishes microseconds. A record arriving between two selectable
  // milliseconds first becomes eligible at the later one.
  const remainder = /\.\d{3}(\d+)/.exec(iso)?.[1] ?? '';
  return floor + (/[1-9]/.test(remainder) ? 1 : 0);
}

function archiveOrigin(label) {
  if (label === 'PILOT news: prices unavailable (no price input connected)') return 'recorded';
  if (label === 'SYNTHETIC fixture data (not real news or market data)') return 'synthetic';
  throw Error('Unrecognized news archive provenance.');
}

export function headlinesAt(pages, selection) {
  const rows = [];
  for (const page of pages) {
    if (page.mode !== 'archive' || page.stream !== 'all') throw Error('Expected an all-stream archive view.');
    const origin = archiveOrigin(page.data_label);
    for (const item of page.rows) {
      if (!item.archived_at || !item.observed_at) continue;
      const archived = Date.parse(item.archived_at);
      if (!Number.isFinite(archived)) throw Error('Invalid archive timestamp.');
      if (sessionAt(archived) !== selection.session) continue;
      const availableAt = Math.max(availableMillisecond(item.archived_at), availableMillisecond(item.event_at));
      rows.push({source:'news-archive', session:selection.session, instrument:selection.instrument,
        id:item.item_id, revision:item.revision_number, eventAt:item.observed_at, availableAt,
        origin, evidence:item.revision_id, value:{title:item.title, source_id:item.source_id, outlet:item.outlet || item.source_id}});
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
    const views = index.views.filter(v => v.date === date && v.mode === 'archive' && v.stream === 'all');
    if (!views.length) { partial = true; continue; }
    if (views.length !== 1) throw Error('Ambiguous news archive view.');
    const [view] = views;
    const keyFor = n => 'news/v2/views/p/'+index.gen+'/archive/all/'+date+'/page-'+String(n).padStart(4,'0')+'.json';
    if (view.first_page !== keyFor(1) || !Number.isSafeInteger(view.page_count) || view.page_count < 1 ||
        view.page_count > panel.MAX_PAGES || ![view.rows_total,view.rows_omitted,view.pages_omitted].every(n=>Number.isSafeInteger(n)&&n>=0))
      throw Error('Invalid news archive view.');
    partial ||= !view.complete || !view.coverage_complete || view.rows_omitted > 0 || view.pages_omitted > 0;
    let rowCount = 0;
    for (let number = 1; number <= view.page_count; number++) {
      if (!leaseValid()) throw Error('News archive lease expired.');
      const page = await fetchJson(keyFor(number));
      if (!leaseValid() || panel.validatePage(page).length || page.gen !== index.gen || page.date !== date ||
          page.mode !== 'archive' || page.stream !== 'all' || page.page_number !== number || page.page_count !== view.page_count ||
          page.prev_page !== (number > 1 ? keyFor(number-1) : null) ||
          page.next_page !== (number < view.page_count ? keyFor(number+1) : null) ||
          page.rows_offset !== rowCount || page.rows_on_page !== page.rows.length ||
          ['rows_total','rows_omitted','pages_omitted','complete'].some(k=>page.view[k] !== view[k]) ||
          !Number.isFinite(Date.parse(page.as_of))) throw Error('Invalid news archive page.');
      rowCount += page.rows.length;
      pages.push(page);
      if ((archiveOrigin(page.data_label)) !== selection.origin) partial = true;
      partial ||= !page.complete || !page.coverage.coverage_complete || Date.parse(page.as_of) < Math.min(selection.cutoff, Date.parse(date+'T23:59:59.999Z'));
    }
    if (rowCount + view.rows_omitted !== view.rows_total) throw Error('News archive row count mismatch.');
  }
  return {rows: headlinesAt(pages, selection), partial};
}

export function followNews({container, research, panel, fetchJson, mountLive, dashboard, events}) {
  const live = document.createElement('div');
  const replay = document.createElement('div');
  container.append(live, replay);
  let generation = 0, showing, scope;
  let cache = new Map(), lease, expiryTimer;
  function refreshControl(reason) {
    generation++;
    clearTimeout(expiryTimer);
    lease = null;
    cache.clear();
    const message = document.createElement('p');
    message.textContent = reason;
    const button = document.createElement('button');
    button.textContent = 'Reload archive at this cursor';
    button.onclick = () => change(dashboard.snapshot());
    replay.replaceChildren(message, button);
  }
  function checkLease() {
    if (showing === 'replay' && lease && !lease()) refreshControl('News archive expired at this cursor.');
  }
  for (const event of ['pageshow','online']) events.addEventListener(event, checkLease);
  function visibilityChanged() {
    change(dashboard.snapshot());
  }
  new MutationObserver(visibilityChanged).observe(container, {attributes: true, attributeFilter: ['class']});
  document.addEventListener('visibilitychange', visibilityChanged);
  function cached(key, signal) {
    if (signal.aborted) return Promise.reject(signal.reason);
    const current = cache;
    if (!current.has(key)) current.set(key, fetchJson(key, signal).catch(error => {current.delete(key); throw error;}));
    return current.get(key);
  }
  async function change(selection) {
    const next = document.hidden || container.classList.contains('hidden') ? 'hidden' : selection.mode;
    if (next === showing && next !== 'replay') return;
    const ticket = ++generation;
    if (next !== showing) {
      scope?.abort();
      clearTimeout(expiryTimer);
      cache = new Map();
      lease = null;
      live.replaceChildren();
      replay.replaceChildren();
      showing = next;
      scope = next === 'hidden' ? null : new AbortController();
    }
    live.hidden = showing !== 'live';
    replay.hidden = showing !== 'replay';
    if (research) research.hidden = showing !== 'live';
    if (showing === 'hidden') return;
    if (showing === 'live') {
      await mountLive(live, scope.signal);
      return;
    }
    replay.textContent = 'Reading archived headlines…';
    if (!lease || !lease()) {
      cache = new Map();
      clearTimeout(expiryTimer);
      lease = panel.startLease({wall:Date.now, mono:() => performance.now()},panel.LEASE_MS);
      expiryTimer = setTimeout(checkLease, panel.LEASE_MS + 1);
    }
    try {
      const signal = scope.signal;
      const result = await archiveNews(selection, key => cached(key, signal), panel, lease);
      if (ticket !== generation) return;
      if (!lease()) { checkLease(); return; }
      const heading = document.createElement('p');
      heading.textContent = 'Market-wide archived news through '+new Date(selection.cutoff).toISOString()+
        ' · '+result.rows.length+' retained headlines'+(result.partial?' · partial archive coverage':'')+'.';
      replay.replaceChildren(heading);
      for (const row of result.rows) {
        const line = document.createElement('p');
        line.textContent = new Date(row.availableAt).toISOString()+' · '+row.value.outlet+' · '+row.value.title;
        line.append(' ');
        panel.appendHeadlineTopics(document, line, row.value);
        replay.append(line);
      }
    } catch (error) {
      if (ticket === generation) refreshControl('News unavailable at this cursor. '+error.message);
    }
  }
  events.addEventListener('one-clock-selection', event => change(event.detail));
  return change(dashboard.snapshot());
}
