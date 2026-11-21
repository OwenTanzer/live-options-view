const q = document.querySelector('#q');
const publisher = document.querySelector('#pub');
const channel = document.querySelector('#ch');
const action = document.querySelector('#act');
let rows = [];
function filter() {
  const terms = q.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
  let count = 0;
  for (const {row, text} of rows) {
    row.hidden = Boolean((publisher.value && row.dataset.publisher !== publisher.value) ||
      (channel.value && row.dataset.channel !== channel.value) ||
      (action.value && row.dataset.action !== action.value) || !terms.every(term => text.includes(term)));
    if (!row.hidden) count++;
  }
  document.querySelector('#count').textContent = count + ' / ' + rows.length;
  document.querySelector('nav').hidden = count !== rows.length;
}
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
        for (const selector of ['header p','nav','tbody']) document.querySelector(selector).replaceWith(doc.querySelector(selector));
        document.querySelector('main').dataset.cursor = main.dataset.cursor;
        index();
      }
      status.textContent = '';
    } catch {
      status.textContent = 'Refresh unavailable. Showing the last loaded window. Reload to sign in again.';
    }
  }
  setTimeout(refresh, 60000);
}
q.addEventListener('input', filter);
publisher.addEventListener('change', filter);
channel.addEventListener('change', filter);
action.addEventListener('change', filter);
document.querySelector('#controls').hidden = false;
index();
setTimeout(refresh, 60000);
