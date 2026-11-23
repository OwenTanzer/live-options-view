// All renderers receive the same cursor window. Only the inspector pins a snapshot.
const dollars = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 2,
});
const premium = (p, multiplier) => p.price * p.size * multiplier;
const isTicket = (p, multiplier) => premium(p, multiplier) >= 10000;
const element = (tag, value) => {
  const node = document.createElement(tag);
  if (value != null) node.textContent = value;
  return node;
};
function button(label, action, className = '') {
  const node = element('button', label);
  node.className = className;
  node.onclick = action;
  return node;
}
const timestamp = (p, clock) =>
  clock(p.t) + '.' + new Date(p.t).getUTCMilliseconds().toString().padStart(3, '0');
const printKey = (p) => [p.symbol, p.t, p.seq ?? p.archiveRow ?? p.publishedRow].join('/');
const side = (p) => (['buy', 'sell'].includes(p.side) ? 'inferred ' + p.side : 'unknown side');

function sales({ panel, rows, clock, multiplier, inspect, all, gaps, spots }) {
  const list = element('div');
  list.className = 'flow-scroll';
  const events = rows.map((p) => ({ t: p.t, p }));
  for (const gap of gaps)
    events.push({ t: gap.end, note: '▨ Missing feed · ' + clock(gap.start) + '–' + clock(gap.end) + ' ET' });
  for (let i = 1; i < spots.length; i++) {
    const prev = spots[i - 1],
      next = spots[i];
    if (
      Math.floor(prev.spot) !== Math.floor(next.spot) &&
      !gaps.some((g) => g.start <= next.t && g.end >= prev.t)
    )
      events.push({
        t: next.t,
        note: clock(next.t) + ' ET · QQQ observed $' + prev.spot + ' → $' + next.spot,
      });
  }
  events.sort((a, b) => a.t - b.t).reverse();
  let small = [];
  function flush() {
    if (!small.length) return;
    const group = small;
    const low = group.reduce((v, p) => Math.min(v, p.strike), Infinity);
    const high = group.reduce((v, p) => Math.max(v, p.strike), -Infinity);
    const sum = group.reduce((n, p) => n + premium(p, multiplier), 0);
    const fold = button(
      '▸ ' + group.length + ' smaller prints · $' + low + '–' + high + ' · ' + dollars.format(sum),
      () => inspect(group, 'Smaller prints'),
      'flow-line flow-group',
    );
    fold.dataset.printKey = printKey(group[0]);
    list.append(fold);
    small = [];
  }
  const largest = rows.reduce((v, p) => Math.max(v, premium(p, multiplier)), 1);
  for (const { p, note } of events) {
    if (p && !all && !isTicket(p, multiplier)) {
      small.push(p);
      continue;
    }
    flush();
    if (note) {
      list.append(element('p', note));
      continue;
    }
    const ticket = isTicket(p, multiplier);
    const row = button(
      timestamp(p, clock) +
        '  ' +
        p.strike +
        p.type +
        '  ' +
        dollars.format(premium(p, multiplier)) +
        '  ' +
        side(p),
      () => inspect([p], 'Print details'),
      'flow-line' + (ticket ? ' ticket' : ''),
    );
    if (ticket) {
      const bar = element('meter');
      bar.min = 0;
      bar.max = largest;
      bar.value = premium(p, multiplier);
      bar.setAttribute('aria-label', 'Ticket premium relative to largest visible ticket');
      row.append(' ', bar);
    }
    row.dataset.printKey = printKey(p);
    list.append(row);
  }
  flush();
  panel.append(list);
}

function chart({ panel, rows, clock, multiplier, inspect, spots, gaps, start, end }) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  const width = Math.max(300, panel.clientWidth),
    height = 320;
  svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
  svg.setAttribute('class', 'flow-chart');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'QQQ observations and option prints by time and strike');
  function mark(tag, attrs, label) {
    const node = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (label) node.textContent = label;
    svg.append(node);
    return node;
  }
  const prices = rows.map((p) => p.strike).concat(spots.map((p) => p.spot));
  const low = prices.reduce((a, b) => Math.min(a, b), Infinity) - 0.5;
  const high = prices.reduce((a, b) => Math.max(a, b), -Infinity) + 0.5;
  const x = (t) => 50 + ((width - 70) * (t - start)) / (end - start);
  const y = (price) => 18 + (260 * (high - price)) / (high - low);
  for (const g of gaps)
    mark('rect', {
      x: x(g.start),
      y: 10,
      width: Math.max(2, x(g.end) - x(g.start)),
      height: 274,
      fill: '#504533',
    });
  for (let i = 0; prices.length && i <= 4; i++) {
    const value = low + (i * (high - low)) / 4;
    mark('line', { x1: 48, x2: width - 10, y1: y(value), y2: y(value), stroke: '#34454b' });
    mark('text', { x: 0, y: y(value) + 4, fill: '#d0dcdd', 'font-size': 12 }, '$' + value.toFixed(1));
  }
  for (const t of [start, end])
    mark(
      'text',
      { x: x(t), y: 307, fill: '#c0ced1', 'text-anchor': t === start ? 'start' : 'end', 'font-size': 12 },
      clock(t) + ' ET',
    );
  let ticks = '';
  for (const p of rows) if (!isTicket(p, multiplier)) ticks += 'M' + x(p.t) + ' ' + y(p.strike) + 'v3';
  mark('path', { d: ticks, stroke: '#87959a', opacity: 0.55, fill: 'none' });
  spots.forEach((p, i) => {
    const prev = spots[i - 1];
    if (prev && !gaps.some((g) => g.start <= p.t && g.end >= prev.t))
      mark('line', {
        x1: x(prev.t),
        x2: x(p.t),
        y1: y(prev.spot),
        y2: y(p.spot),
        stroke: '#ffd579',
        'stroke-width': 2,
      });
    mark('circle', { cx: x(p.t), cy: y(p.spot), r: 3, fill: '#ffd579' });
  });
  for (const p of rows)
    if (isTicket(p, multiplier)) {
      const cx = x(p.t),
        cy = y(p.strike),
        r = Math.sqrt(premium(p, multiplier) / 10000) * 4;
      mark('circle', { cx, cy, r, fill: '#628ea7', 'fill-opacity': 0.35, stroke: '#a4cfea' });
      mark(
        'text',
        { x: cx, y: cy + 4, fill: 'white', 'text-anchor': 'middle', 'font-size': 12 },
        p.type === 'C' ? '▴' : '▾',
      );
    }
  svg.onclick = (event) => {
    const pt = new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.getScreenCTM().inverse());
    const nearby = rows.filter(
      (p) =>
        Math.hypot(x(p.t) - pt.x, y(p.strike) - pt.y) <=
        Math.max(10, isTicket(p, multiplier) ? Math.sqrt(premium(p, multiplier) / 10000) * 4 : 0),
    );
    inspect(nearby.slice().reverse(), 'Prints at chart position');
  };
  panel.append(
    svg,
    element(
      'p',
      spots.length
        ? 'Gold dots are QQQ chain observations. Lines connect observations.'
        : 'QQQ price unavailable in this minute.',
    ),
    button('Inspect chart prints (' + rows.length + ')', () =>
      inspect(rows.slice().reverse(), 'Chart prints'),
    ),
  );
}

function footprint({ panel, rows, clock, multiplier, inspect, spots }) {
  const buckets = new Map(),
    strikes = [...new Set(rows.map((p) => p.strike))].sort((a, b) => a - b);
  for (const p of rows) {
    const time = Math.floor(p.t / 10000) * 10000;
    if (!buckets.has(time)) buckets.set(time, new Map());
    const cells = buckets.get(time);
    if (!cells.has(p.strike)) cells.set(p.strike, []);
    cells.get(p.strike).push(p);
  }
  const wrap = element('div'),
    table = element('table'),
    head = table.createTHead().insertRow();
  wrap.className = 'flow-scroll';
  head.append(element('th', 'ET / strike'));
  for (const strike of strikes) head.append(element('th', '$' + strike));
  const body = table.createTBody();
  for (const [time, cells] of [...buckets].sort((a, b) => b[0] - a[0])) {
    const tr = body.insertRow();
    tr.append(element('th', clock(time)));
    const spot = spots.filter((p) => p.t >= time && p.t < time + 10000).at(-1);
    for (const strike of strikes) {
      const td = tr.insertCell(),
        prints = cells.get(strike);
      if (spot && Math.round(spot.spot) === strike) {
        td.className = 'spot-cell';
        td.title = 'QQQ observed at $' + spot.spot;
      }
      if (!prints) {
        td.textContent = '—';
        continue;
      }
      const sums = { buy: 0, sell: 0, unknown: 0 };
      for (const p of prints) sums[p.side in sums ? p.side : 'unknown'] += p.size;
      const ticket = prints.some((p) => isTicket(p, multiplier));
      const cell = button((ticket ? '◆ ' : '') + (sums.buy + sums.sell + sums.unknown), () =>
        inspect(prints.slice().reverse(), clock(time) + ' ET · $' + strike),
      );
      cell.append(element('small', sums.buy + ' / ' + sums.sell + ' / ' + sums.unknown));
      cell.dataset.printKey = time + '/' + strike;
      td.append(cell);
    }
  }
  wrap.append(table);
  panel.append(wrap);
}

const views = [
  { id: 'classic', label: 'Current tape' },
  {
    id: 'sales',
    label: 'A · Time & Sales',
    hint: 'Newest first · premium per ticket · $10k+ highlighted',
    draw: sales,
  },
  {
    id: 'chart',
    label: 'B · QQQ price chart',
    hint: 'Strike and QQQ price ($) · circle area = premium · ▴ call ▾ put · smaller prints are ticks',
    draw: chart,
  },
  {
    id: 'footprint',
    label: 'C · Strike footprint',
    hint: '10-second intervals · contracts traded · inferred buy / sell / unknown · ◆ $10k+ ticket · gold edge = observed QQQ rounded to strike',
    draw: footprint,
  },
];

export function createTapeViews(el, clock, available, getMultiplier) {
  const select = el('tape-view'),
    root = el('optional-tape'),
    dialog = el('print-inspector');
  const panel = element('div'),
    toggle = element('input'),
    label = element('label');
  const heading = element('h2'),
    hint = element('p'),
    coverage = element('p');
  toggle.type = 'checkbox';
  label.append(toggle, ' All prints');
  root.append(heading, hint, label, coverage, panel);
  for (const v of views) select.add(new Option(v.label, v.id));
  let current = views[0],
    key = null,
    snapshot = [],
    page = 0;
  const size = 60;
  function showPage() {
    const list = dialog.querySelector('ol');
    list.replaceChildren();
    list.start = page * size + 1;
    for (const p of snapshot.slice(page * size, (page + 1) * size)) {
      const contract = window.OrderFlowModel.parseSymbol(p.symbol);
      const id =
        p.seq != null
          ? 'source sequence ' + p.seq
          : p.archiveRow != null
            ? 'archive row ' + p.archiveRow
            : 'published row ' + p.publishedRow;
      list.append(
        element(
          'li',
          timestamp(p, clock) +
            ' ET · ' +
            p.symbol +
            ' · ' +
            (contract?.expiration || 'expiry unavailable') +
            ' · $' +
            p.strike +
            ' ' +
            (p.type === 'C' ? 'call' : 'put') +
            ' · ' +
            p.size +
            ' × ' +
            dollars.format(p.price) +
            ' · ' +
            dollars.format(premium(p, getMultiplier())) +
            ' premium · ' +
            side(p) +
            ' · ' +
            id,
        ),
      );
    }
    list.scrollTop = 0;
    el('inspect-context').textContent = snapshot.length
      ? list.start +
        '–' +
        Math.min(snapshot.length, (page + 1) * size) +
        ' of ' +
        snapshot.length +
        ' prints at selection'
      : 'No matching prints at this selection.';
    el('inspect-prev').disabled = page === 0;
    el('inspect-next').disabled = (page + 1) * size >= snapshot.length;
  }
  function inspect(rows, title) {
    snapshot = rows;
    page = 0;
    el('inspector-title').textContent = title;
    showPage();
    dialog.showModal();
  }
  el('inspect-available').onclick = () =>
    inspect(available(), 'Available prints through cursor · contract filter');
  el('inspect-prev').onclick = () => {
    page--;
    showPage();
  };
  el('inspect-next').onclick = () => {
    page++;
    showPage();
  };
  el('inspect-close').onclick = () => dialog.close();
  dialog.onclose = () => {
    snapshot = [];
  };
  select.onchange = () => {
    current = views.find((v) => v.id === select.value);
    key = null;
    for (const id of ['classic-tape', 'tape-legend']) el(id).hidden = !!current.draw;
    root.hidden = !current.draw;
    heading.textContent = current.label;
    hint.textContent = current.hint || '';
    label.hidden = current.draw !== sales;
    select.dispatchEvent(new Event('input'));
  };
  toggle.onchange = () => {
    key = null;
    select.dispatchEvent(new Event('input'));
  };
  return {
    optional: () => !!current.draw,
    reset() {
      key = null;
      if (dialog.open) dialog.close();
    },
    render(rows, spots, rawGaps, start, end) {
      const gaps = rawGaps
        .map((g) => ({ start: Math.max(start, g.start ?? start), end: Math.min(end, g.end ?? end) }))
        .filter((g) => g.end > g.start);
      const next = JSON.stringify([
        Math.floor(end / 1000),
        panel.clientWidth,
        rows.map((p) => [p.t, p.seq, p.symbol, p.price, p.size, p.side]),
        spots,
        gaps,
      ]);
      if (next === key || dialog.open) return;
      key = next;
      const old = panel.querySelector('.flow-scroll'),
        top = old?.scrollTop,
        left = old?.scrollLeft;
      const focused = panel.contains(document.activeElement) ? document.activeElement.dataset.printKey : null;
      panel.replaceChildren();
      coverage.textContent = [
        rows.length + ' prints in the displayed minute',
        ...gaps.map((g) => '▨ Missing feed ' + clock(g.start) + '–' + clock(g.end) + ' ET'),
      ].join(' · ');
      current.draw({
        panel,
        rows,
        spots,
        gaps,
        start,
        end,
        clock,
        multiplier: getMultiplier(),
        inspect,
        all: toggle.checked,
      });
      if (focused) {
        const target = Array.from(panel.querySelectorAll('[data-print-key]')).find(
          (node) => node.dataset.printKey === focused,
        );
        (target || select).focus({ preventScroll: true });
      }
      const scroll = panel.querySelector('.flow-scroll');
      if (scroll) {
        scroll.scrollTop = top || 0;
        scroll.scrollLeft = left || 0;
      }
    },
  };
}
