const q = document.querySelector('#q');
const publisher = document.querySelector('#pub');
const channel = document.querySelector('#ch');
const action = document.querySelector('#act');
const more = document.querySelector('#more');
const batchSize = 100;
let visibleLimit = batchSize;
let rows = [];
function filter() {
  const terms = q.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
  let count = 0;
  for (const {row, text} of rows) {
    const excluded = Boolean((publisher.value && row.dataset.publisher !== publisher.value) ||
      (channel.value && row.dataset.channel !== channel.value) ||
      (action.value && row.dataset.action !== action.value) || !terms.every(term => text.includes(term)));
    row.hidden = excluded || count >= visibleLimit;
    if (!excluded) count++;
  }
  document.querySelector('#count').textContent = Math.min(count, visibleLimit) + ' of ' + count + ' matching reports';
  more.hidden = count <= visibleLimit;
  document.querySelector('nav').hidden = count !== rows.length || !more.hidden;
}
const columns = document.querySelector('#columns');
function applyColumns() {
  const selected = [...columns.querySelectorAll('input:checked')].map(input => input.value);
  const table = document.querySelector('table');
  table.parentElement.classList.toggle('extended', selected.length > 0);
  for (const cell of table.querySelectorAll('.optional')) cell.remove();
  if (!selected.length) return;
  const header = table.tHead.rows[0];
  for (const label of selected) {
    const th = document.createElement('th');
    th.className = 'optional';
    th.textContent = label;
    header.append(th);
  }
  for (const {row} of rows) {
    const fields = new Map([...row.querySelectorAll('dt')].map(term =>
      [term.textContent, term.nextElementSibling.textContent]));
    for (const label of selected) {
      const cell = row.insertCell();
      cell.className = 'optional';
      cell.textContent = fields.get(label) || '—';
    }
  }
}
columns.hidden = false;
columns.addEventListener('change', applyColumns);

function index() {
  rows = [...document.querySelectorAll('tbody tr')].map(row => ({
    row, text: ([...row.cells].map(c => c.innerText).join(' ') + ' ' + row.dataset.search).toLowerCase()
  }));
  for (const [select, key] of [[publisher, 'publisher'], [channel, 'channel'], [action, 'action']]) {
    const saved = select.value;
    const values = new Set(rows.map(item => item.row.dataset[key]));
    if (saved) values.add(saved);
    select.replaceChildren(new Option('All',''), ...[...values].sort().map(v => new Option(v,v)));
    select.value = saved;
  }
  applyColumns();
  filter();
}
async function refresh() {
  if (!document.hidden) {
    const status = document.querySelector('#status');
    try {
      const response = await fetch('/credit-tape/' + location.search, {cache:'no-store',redirect:'error'});
      if (!response.ok) throw new Error();
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const main = doc.querySelector('main[data-cursor]');
      if (!main) {
        if (doc.querySelector('a[href="/credit-tape/auth/login"]')) {
          location.reload();
          return;
        }
        throw new Error();
      }
      if (main.dataset.cursor !== document.querySelector('main').dataset.cursor) {
        const expanded = new Set([...document.querySelectorAll('tbody details[open]')].map(el => el.dataset.key));
        for (const selector of ['header p','nav','tbody']) document.querySelector(selector).replaceWith(doc.querySelector(selector));
        document.querySelector('main').dataset.cursor = main.dataset.cursor;
        for (const el of document.querySelectorAll('details[data-key]')) {
          if (expanded.has(el.dataset.key)) el.open = true;
        }
        index();
      }
      status.textContent = '';
    } catch {
      status.textContent = 'Refresh unavailable. Showing the last loaded window. Reload to sign in again.';
    }
  }
  setTimeout(refresh, 60000);
}
for (const control of [q, publisher, channel, action]) {
  control.addEventListener(control === q ? 'input' : 'change', () => {
    visibleLimit = batchSize;
    filter();
  });
}
more.addEventListener('click', () => {
  visibleLimit += batchSize;
  filter();
});
document.querySelector('#controls').hidden = false;
index();
const initialPublisher = document.querySelector('main').dataset.publisherFilter;
if (initialPublisher) {
  if (![...publisher.options].some(option => option.value === initialPublisher)) {
    publisher.add(new Option(initialPublisher, initialPublisher));
  }
  publisher.value = initialPublisher;
  filter();
}
if (document.querySelector('main').dataset.mode !== 'replay') setTimeout(refresh, 60000);
