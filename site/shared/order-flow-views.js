// All renderers receive the same cursor window. Only the inspector pins a snapshot.
const dollars = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 2,
});
const premium = (p, multiplier) => p.price * p.size * multiplier;
const element = (tag, value) => {
  const node = document.createElement(tag);
  if (value != null) node.textContent = value;
  return node;
};
const timestamp = (p, clock) =>
  clock(p.t) + '.' + new Date(p.t).getUTCMilliseconds().toString().padStart(3, '0');
const side = (p) => (['buy', 'sell'].includes(p.side) ? 'inferred ' + p.side : 'unknown side');

export function createTapeViews(el, formatClock, available, getMultiplier) {
  const root = el('optional-tape'),
    dialog = el('print-inspector');
  const shadow = root.attachShadow({ mode: 'open' });
  shadow.append(el('tape-design').content.cloneNode(true));
  const $ = (selector) => shadow.querySelector(selector);
  let snapshot = [],
    page = 0;
  const clock = formatClock;
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

  $('#spot-source').textContent = 'QQQ prices and crossings use available chain snapshots.';
  const design = createDesign(shadow, formatClock, getMultiplier, inspect);
  let current = 'classic',
    key = '',
    held = false;
  const toggle = $('.controls input');
  function repaint() {
    key = '';
    el('tape-view').dispatchEvent(new Event('input'));
  }
  function hold() {
    held = true;
    $('#resume').hidden = false;
  }
  function resume() {
    held = false;
    design.reset();
    $('#resume').hidden = true;
    repaint();
  }
  $('#resume').onclick = resume;
  toggle.onchange = resume;
  $('#view').addEventListener('click', (e) => {
    if (e.target.closest('#dlive')) resume();
    else hold();
  });
  $('#view').addEventListener('input', hold);
  $('#view').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') hold();
  });
  for (const [id, name] of Object.entries(design.options)) {
    const b = element('button', name);
    b.dataset.view = id;
    b.setAttribute('aria-pressed', id === current);
    el('tape-view').append(b);
    b.onclick = () => {
      current = id;
      const optional = id !== 'classic';
      for (const item of el('tape-view').children) item.setAttribute('aria-pressed', item === b);
      for (const id of ['classic-tape', 'tape-legend']) el(id).hidden = optional;
      document.body.classList.toggle('alternate-tape', optional);
      root.hidden = !optional;
      toggle.parentElement.hidden = id !== 'sales';
      design.reset();
      resume();
    };
  }
  const linkedView = new URLSearchParams(location.search).get('tape');
  if (Object.hasOwn(design.options, linkedView))
    el('tape-view')
      .querySelector('[data-view="' + linkedView + '"]')
      .click();
  for (const id of ['filter', 'strike-center', 'zoom-in', 'zoom-out', 'follow-spot', 'seek'])
    for (const event of ['input', 'change', 'click']) el(id).addEventListener(event, resume);
  return {
    optional: () => current !== 'classic',
    reset() {
      design.reset();
      resume();
      if (dialog.open) dialog.close();
    },
    render(rows, spots, gaps, start, end, strikes, cappedFrom) {
      if (held || dialog.open) return;
      const next = JSON.stringify([
        Math.floor(end / 1000),
        root.clientWidth,
        rows.map((p) => [p.t, p.seq, p.archiveRow, p.price, p.size, p.symbol, p.side]),
        spots,
        gaps,
        strikes,
        cappedFrom,
      ]);
      if (next === key) return;
      key = next;
      design.render(current, rows, spots, gaps, start, end, strikes, toggle.checked, cappedFrom);
    },
  };
}

function createDesign(shadow, formatClock, getMultiplier, inspect) {
  const $ = (s) => shadow.querySelector(s);
  let K = [],
    all = false;
  const BIG = 10000;
  function clock(S, rel, ms = false) {
    const t = S.t0 + rel;
    return formatClock(t) + (ms ? '.' + String(new Date(t).getUTCMilliseconds()).padStart(3, '0') : '');
  }
  const prem = (p) => p[3] * p[4] * getMultiplier();
  const money = (v) =>
    v >= 1e6
      ? '$' + (v / 1e6).toFixed(2) + 'M'
      : v >= 1e3
        ? '$' + (v / 1e3).toFixed(v >= 1e5 ? 0 : 1) + 'k'
        : '$' + Math.round(v);
  const side = { b: ['buy', 'buy'], s: ['sell', 'sell'], u: ['side ?', 'unk'] };
  const ctr = (p) => `${K[p[1]]} ${esc(p[2])}`;
  const esc = (s) =>
    String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
  function printLine(S, p) {
    const row = p[6],
      expiry = window.OrderFlowModel.parseSymbol(row.symbol)?.expiration ?? 'unavailable';
    const id =
      row.seq != null
        ? 'source sequence ' + row.seq
        : row.archiveRow != null
          ? 'archive row ' + row.archiveRow
          : 'published row ' + row.publishedRow;
    return `<b>${clock(S, p[0], true)} ET</b> · <b>${ctr(p)}</b> · exp ${esc(expiry)} · $${p[3].toFixed(2)} × ${p[4]} = <b>${money(prem(p))}</b> · ${side[p[5]][0]} (inferred side) · ${esc(id)}`;
  }
  function spotAt(S, rel) {
    const p = S.points.findLast((p) => p.t <= rel);
    return p && !S.gaps.some(([a, b]) => a <= rel && b >= p.t) ? p.spot : null;
  }
  const OPT = {
    A: [
      'A · Time & Sales, tickets first',
      "A trader's tape, newest at top. Every ticket of $10k+ gets its own bold row. The small prints between tickets fold into one grey line you can open. QQQ crossing a strike and feed gaps are rows in the tape.",
    ],
    B: [
      'B · QQQ price chart with tickets pinned on it',
      'The QQQ line is the main object, on the same price axis as the strikes. Tickets are circles at their strike and time (area = premium, ▲ call ▼ put). Every small print is a faint tick. Tap anywhere to list the prints there.',
    ],
    C: [
      'C · Strike footprint',
      'Rows are 10-second slices (newest at top), columns are strikes. Each cell shows contracts traded, with buy/sell underneath. Bold underlined = contains a $10k+ ticket. A yellow edge marks where QQQ was. Tap a cell for its prints.',
    ],
    D: [
      'D · Mirrored strike ladder, right now',
      'One row per strike, with QQQ as a line through the ladder. Call premium grows left, put premium grows right, over the last 60 seconds; the last 10 seconds are bright. Tickets are chips on the bars. Drag the time slider to move "now"; nothing after it is shown.',
    ],
    E: [
      'E · What just happened, in sentences',
      'Prints are grouped into events: the same contract and side, each print within 1 second of the last, at most 3 seconds in all. Every event of $10k+ is one plain sentence, newest at top. Everything smaller is summed into a background line every 15 seconds. Tap any sentence for its prints.',
    ],
  };
  const GROUP_MS = 1000,
    GROUP_SPAN = 3000,
    D_WIN = 60000,
    D_HOT = 10000;

  function renderNow(S) {
    const last = spotAt(S, S.duration),
      first = S.points[0]?.spot;
    const tot = S.prints.reduce((a, p) => a + prem(p), 0),
      big = S.prints.filter((p) => prem(p) >= BIG).length;
    const capNote =
      S.cappedFrom == null
        ? ''
        : S.cappedFrom > S.t0 + S.duration
          ? 'Outside retained prints (display cap)'
          : 'Covers only from ' + formatClock(S.cappedFrom) + ' ET (display cap)';
    $('#now').innerHTML =
      `<span class="q">QQQ ${last == null ? 'unavailable' : last.toFixed(2)}</span><span class="t">${S.start}–${S.end} ET · from ${first == null ? 'unavailable' : first.toFixed(2)}</span><span class="t">${S.prints.length.toLocaleString()} prints · ${money(tot)} · ${big} tickets ≥ $10k</span>` +
      (capNote ? `<b class="coverage-cap">${capNote}</b>` : '');
  }

  /* ---------- A: Time & Sales ---------- */
  function renderA(S) {
    const P = S.prints,
      rows = [];
    const lost = S.gaps.map((g) => ({ t: g[0], end: g[1] }));
    let group = [],
      lastT = S.cappedFrom == null ? 0 : S.cappedFrom - S.t0;
    const flush = () => {
      if (group.length) rows.push({ k: 'run', ps: group, t: group[group.length - 1][0] });
      group = [];
    };
    for (const c of crossings(S))
      rows.push({ k: 'cross', t: c.t, txt: `QQQ crosses ${c.k} ${c.up ? '↑' : '↓'} · ${c.v.toFixed(2)}` });
    for (const g of lost) rows.push({ k: 'lost', t: g.t, g });
    for (let i = 0; i < P.length; i++) {
      const p = P[i];
      const pause = p[0] - lastT;
      if (pause > 8000 && !lost.some((g) => g.t < p[0] && g.end > lastT)) {
        flush();
        rows.push({
          k: 'quiet',
          t: lastT + 1,
          txt: `No prints for ${(pause / 1000).toFixed(1)} s · in the displayed tape`,
        });
      } else if (pause > 8000) {
        const g = lost.find((g) => g.t < p[0] && g.end > lastT);
        if (g.t - lastT > 3000) {
          flush();
          rows.push({
            k: 'quiet',
            t: lastT + 1,
            txt: `No prints for ${((g.t - lastT) / 1000).toFixed(1)} s · in the displayed tape`,
          });
        }
      }
      if (all || prem(p) >= BIG) {
        flush();
        rows.push({ k: 'big', p, t: p[0] });
      } else {
        if (group.length && p[0] - group[0][0] > 10000) flush();
        group.push(p);
      }
      lastT = p[0];
    }
    flush();
    rows.forEach((r, i) => (r.o = r.k === 'cross' || r.k === 'lost' ? -1 : i));
    rows.sort((a, b) => b.t - a.t || b.o - a.o);
    const maxP = P.reduce((v, p) => Math.max(v, prem(p)), 1);
    const w = (v) => Math.max(4, Math.sqrt(v / maxP) * 100);
    let h = '';
    rows.forEach((r, i) => {
      if (r.k === 'big') {
        const p = r.p;
        h += `<div class="row ${prem(p) >= BIG ? 'big' : ''}" data-record="${i}" role="button" tabindex="0"><span class="tm">${clock(S, p[0], true)}</span><span class="ctr">${ctr(p)}</span><span class="bar" style="width:${w(prem(p))}%;background:var(--${side[p[5]][1]})"></span><span class="prem">${money(prem(p))}</span><span class="side ${side[p[5]][1]}">${side[p[5]][0]}</span></div>`;
      } else if (r.k === 'run') {
        const ps = r.ps,
          v = ps.reduce((a, p) => a + prem(p), 0),
          n = { b: 0, s: 0, u: 0 };
        ps.forEach((p) => (n[p[5]] += p[4]));
        const tc = n.b + n.s + n.u,
          strikes = [...new Set(ps.map((p) => K[p[1]]))].sort((a, b) => a - b);
        h += `<div class="row run" data-i="${i}" role="button" tabindex="0" aria-expanded="false"><span class="tm">${clock(S, ps[ps.length - 1][0])}</span><span class="ctr">▸ ${ps.length} small print${ps.length > 1 ? 's' : ''} · ${strikes.length > 1 ? strikes[0] + '–' + strikes.at(-1) : strikes[0]} · ${tc.toLocaleString()} contracts</span><span class="prem">${money(v)}</span><span class="side"><span class="mix" title="contracts: buy ${n.b}, sell ${n.s}, unknown ${n.u}"><i style="flex:${n.b};background:var(--buy)"></i><i style="flex:${n.s};background:var(--sell)"></i><i style="flex:${n.u};background:var(--faint)"></i></span></span></div><div class="inner" hidden></div>`;
      } else if (r.k === 'cross') h += `<div class="cross">${clock(S, r.t)} · ${r.txt}</div>`;
      else if (r.k === 'lost')
        h += `<div class="lost"><b>Feed lost ${clock(S, r.g.t, true)} → ${clock(S, r.g.end, true)} (${((r.g.end - r.g.t) / 1000).toFixed(1)} s).</b> Prints in this time were not received. This is missing data, not a quiet market.</div>`;
      else h += `<div class="quiet">${clock(S, r.t)} · ${r.txt}</div>`;
    });
    $('#view').innerHTML =
      `<div class="tape">${h}</div><div class="key"><b>Bold row</b> = one ticket ≥ $10k, bar length ∝ √premium, colour = <span class="buy">buy</span> / <span class="sell">sell</span> / <span class="unk">side ?</span> · <b>▸ grey row</b> = the small prints between tickets (up to 10 s), tap to list each one</div>`;
    $('#detail').innerHTML = '';
    $('#view')
      .querySelectorAll('[data-record]')
      .forEach((el) => activate(el, () => inspect([rows[+el.dataset.record].p[6]], 'Print details')));
    $('#view')
      .querySelectorAll('.run')
      .forEach((el) => {
        const open = () => {
          const r = rows[+el.dataset.i],
            box = el.nextElementSibling,
            on = box.hidden;
          box.hidden = !on;
          el.classList.toggle('open', on);
          el.setAttribute('aria-expanded', on);
          el.querySelector('.ctr').innerHTML = el
            .querySelector('.ctr')
            .innerHTML.replace(on ? '▸' : '▾', on ? '▾' : '▸');
          if (on && !box.innerHTML)
            box.innerHTML = r.ps
              .slice()
              .reverse()
              .map((p) => '<div>' + printLine(S, p) + '</div>')
              .join('');
        };
        el.onclick = open;
        el.onkeydown = (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            open();
          }
        };
      });
  }

  /* ---------- B: price chart + tickets ---------- */
  function renderB(S) {
    const box = $('#view');
    box.innerHTML = `<div class="strike-track"><canvas id="cv"></canvas></div><div class="key"><b style="color:var(--spot)">━ QQQ</b> · ● ticket ≥ $10k at its strike (area = premium, ▲ call ▼ put, colour = <span class="buy">buy</span> / <span class="sell">sell</span> / <span class="unk">side ?</span>) · <span style="color:var(--faint)">faint ticks</span> = every other print · <span style="color:#f1c27d">hatched</span> = feed lost</div>`;
    const cv = $('#cv'),
      dpr = devicePixelRatio || 1,
      Wd = cv.clientWidth,
      Hd = Math.min(520, Math.max(180, Math.floor(innerHeight * 0.6)), Math.max(360, Wd * 0.55));
    box.style.setProperty('--chart-height', Hd + 'px');
    cv.width = Wd * dpr;
    cv.height = Hd * dpr;
    cv.style.height = Hd + 'px';
    const c = cv.getContext('2d');
    c.scale(dpr, dpr);
    const L = 44,
      R = 10,
      T = 10,
      B = 26,
      dur = S.duration;
    // Keep the canvas bounded even for a full chain. Native scrolling moves a
    // five-strike price window; ticket picking uses the same projection.
    const count = Math.min(5, K.length);
    const stepPx = 80;
    const travel = (K.length - count) * stepPx;
    $('.strike-track').style.height = Hd + travel + 'px';
    const range = $('#chart-strikes');
    let lo, hi;
    const X = (t) => L + ((Wd - L - R) * t) / dur;
    const Y = (v) => T + ((Hd - T - B) * (hi - v)) / (hi - lo);
    const big = S.prints.filter((p) => prem(p) >= BIG).sort((a, b) => prem(b) - prem(a));
    const col = { b: '#3fd0a4', s: '#f07a64', u: '#9aa6ad' };
    const rad = (p) => 3 + Math.sqrt(prem(p) / 270000) * 16;
    function paint() {
      const offset = Math.min(K.length - count, Math.round(box.scrollTop / stepPx));
      const first = K.length - count - offset;
      const bottomStrike = K[first],
        topStrike = K[first + count - 1];
      const padding = count > 1 ? ((topStrike - bottomStrike) / (count - 1)) * 0.45 : 0.5;
      lo = bottomStrike - padding;
      hi = topStrike + padding;
      range.textContent = 'Strikes $' + bottomStrike + '–$' + topStrike + ' of $' + K[0] + '–$' + K.at(-1);
      c.clearRect(0, 0, Wd, Hd);
      c.save();
      c.beginPath();
      c.rect(0, T, Wd, Hd - T - B);
      c.clip();
      c.font = '12px ui-monospace,Consolas,monospace';
      c.fillStyle = '#8a9aa3';
      c.textAlign = 'right';
      for (const k of K) {
        c.strokeStyle = '#1b252b';
        c.beginPath();
        c.moveTo(L, Y(k));
        c.lineTo(Wd - R, Y(k));
        c.stroke();
        c.fillText(k, L - 6, Y(k) + 4);
      }
      for (const g of S.gaps) {
        const x0 = X(g[0]),
          x1 = X(g[1]);
        c.save();
        c.beginPath();
        c.rect(x0, T, x1 - x0, Hd - T - B);
        c.clip();
        c.fillStyle = 'rgba(197,139,42,.16)';
        c.fillRect(x0, T, x1 - x0, Hd - T - B);
        c.strokeStyle = 'rgba(197,139,42,.5)';
        for (let x = x0 - 200; x < x1; x += 8) {
          c.beginPath();
          c.moveTo(x, Hd - B);
          c.lineTo(x + 200, T);
          c.stroke();
        }
        c.restore();
        c.fillStyle = '#f1c27d';
        c.textAlign = 'left';
        c.fillText(
          'feed lost ' + ((g[1] - g[0]) / 1000).toFixed(1) + ' s',
          Math.min(x1 + 4, Wd - 110),
          T + 14,
        );
      }
      // faint ticks: every print
      c.fillStyle = 'rgba(138,154,163,.28)';
      for (const p of S.prints) {
        if (prem(p) >= BIG) continue;
        const y = Y(K[p[1]]) + (p[2] === 'C' ? -3 : 3);
        c.fillRect(X(p[0]), y - 1.5, 1, 3);
      }
      // spot
      c.strokeStyle = '#ffd166';
      c.lineWidth = 2.2;
      c.beginPath();
      S.points.forEach((p, i) => {
        const x = X(p.t),
          y = Y(p.spot),
          prev = S.points[i - 1];
        if (!prev || S.gaps.some(([a, b]) => a <= p.t && b >= prev.t)) c.moveTo(x, y);
        else c.lineTo(x, y);
      });
      c.stroke();
      c.lineWidth = 1;
      for (const p of S.points) {
        c.fillStyle = '#ffd166';
        c.fillRect(X(p.t) - 1, Y(p.spot) - 1, 3, 3);
      }
      // tickets
      for (const p of big.slice().reverse()) {
        const x = X(p[0]),
          y = Y(K[p[1]]) + (p[2] === 'C' ? -6 : 6),
          r = rad(p);
        c.globalAlpha = 0.85;
        c.fillStyle = col[p[5]];
        c.beginPath();
        c.arc(x, y, r, 0, 7);
        c.fill();
        c.globalAlpha = 1;
        c.fillStyle = '#0b0f12';
        c.font = `${Math.max(8, r)}px system-ui`;
        c.textAlign = 'center';
        if (r > 6) c.fillText(p[2] === 'C' ? '▲' : '▼', x, y + r * 0.35);
      }
      c.font = '12px ui-monospace,Consolas,monospace';
      c.fillStyle = '#e6edf0';
      c.textAlign = 'left';
      for (const p of big.slice(0, 5)) {
        const x = X(p[0]),
          y = Y(K[p[1]]) + (p[2] === 'C' ? -6 : 6);
        c.fillText(money(prem(p)), Math.min(x + rad(p) + 3, Wd - 50), y - rad(p) + 2);
      }
      c.restore();
      c.font = '12px ui-monospace,Consolas,monospace';
      c.fillStyle = '#8a9aa3';
      c.textAlign = 'center';
      const step = dur > 150000 ? 30000 : 20000;
      for (let t = 0; t <= dur; t += step)
        c.fillText(clock(S, t).slice(0, 8), Math.min(Wd - 36, Math.max(L + 30, X(t))), Hd - 6);
    }
    box.onscroll = paint;
    paint();
    cv.setAttribute('aria-label', 'QQQ price and tickets by time and strike');
    const list = document.createElement('button');
    list.textContent = 'Inspect chart prints (' + S.prints.length + ')';
    list.onclick = () => inspect(S.prints.map((p) => p[6]).reverse(), 'Chart prints');
    box.append(list);
    const pick = (e) => {
      const rc = cv.getBoundingClientRect(),
        x = e.clientX - rc.left,
        y = e.clientY - rc.top;
      const t = ((x - L) / (Wd - L - R)) * dur;
      if (t < 0 || t > dur) return;
      const hit = big.find(
        (p) => Math.hypot(X(p[0]) - x, Y(K[p[1]]) + (p[2] === 'C' ? -6 : 6) - y) <= rad(p) + 2,
      );
      if (hit) {
        const hits = big.filter(
          (p) => Math.hypot(X(p[0]) - x, Y(K[p[1]]) + (p[2] === 'C' ? -6 : 6) - y) <= rad(p) + 2,
        );
        inspect(
          hits.map((p) => p[6]),
          'Tickets at chart position',
        );
        return;
      }
      const k = K.reduce((b, v) => (Math.abs(Y(v) - y) < Math.abs(Y(b) - y) ? v : b), K[0]),
        ki = K.indexOf(k);
      const win = 1500,
        ps = S.prints.filter((p) => p[1] === ki && Math.abs(p[0] - t) <= win);
      const g = S.gaps.find((g) => t >= g[0] && t <= g[1]);
      $('#detail').innerHTML =
        (g ? `<div style="color:#f1c27d">Feed lost here: prints were not received.</div>` : '') +
        `<div>${k} strike, ${clock(S, t - win)}–${clock(S, t + win)}: <b>${ps.length}</b> print${ps.length === 1 ? '' : 's'}${ps.length ? ' (newest first)' : ''}</div><div class="list">${ps
          .slice()
          .reverse()
          .map((p) => '<div>' + printLine(S, p) + '</div>')
          .join('')}</div>`;
    };
    cv.onclick = pick;
    $('#detail').innerHTML =
      'Tap a circle for that ticket, or anywhere along a strike to list every print there (±1.5 s).';
  }

  /* ---------- C: footprint ---------- */
  function renderC(S) {
    const bin = 10000,
      nb = Math.ceil(S.duration / bin);
    const cells = Array.from({ length: nb }, () => K.map(() => ({ ps: [], b: 0, s: 0, u: 0, big: false })));
    for (const p of S.prints) {
      const r = Math.min(nb - 1, Math.floor(p[0] / bin)),
        cl = cells[r][p[1]];
      cl.ps.push(p);
      cl[p[5]] += p[4];
      if (prem(p) >= BIG) cl.big = true;
    }
    const maxN = cells.reduce((max, row) => row.reduce((n, c) => Math.max(n, c.b + c.s + c.u), max), 1);
    const ks = K.slice().reverse();
    let h = `<div class="gridwrap"><table class="grid"><thead><tr><th style="width:74px;text-align:left">ET</th>${ks.map((k) => `<th>${k}</th>`).join('')}</tr></thead><tbody>`;
    for (let r = nb - 1; r >= 0; r--) {
      const t0 = r * bin,
        t1 = t0 + bin,
        g = S.gaps.find((g) => g[0] < t1 && g[1] > t0);
      const sv = spotAt(S, t1);
      if (g && g[0] <= t0 && g[1] >= t1) {
        h += `<tr class="lostrow"><td class="t">${clock(S, t0)}</td><td colspan="${K.length}">feed lost — prints not received</td></tr>`;
        continue;
      }
      h += `<tr><td class="t">${clock(S, t0)}${g ? '<br><span style="color:#f1c27d;font-size:11px">part lost</span>' : ''}</td>`;
      for (const k of ks) {
        const ki = K.indexOf(k),
          cl = cells[r][ki],
          n = cl.b + cl.s + cl.u;
        const a = n ? 0.08 + 0.5 * Math.sqrt(n / maxN) : 0;
        const isSpot = sv != null && Math.floor(sv) === k;
        h += `<td data-r="${r}" data-k="${ki}" class="${cl.big ? 'hasbig' : ''} ${isSpot ? 'spotcol' : ''}" style="background:rgba(120,170,190,${a.toFixed(2)})" title="${isSpot ? 'QQQ ' + sv.toFixed(2) + ' (between ' + k + ' and ' + (k + 1) + ')' : ''}">${n ? `<span class="n">${n.toLocaleString()}</span><span class="s"><span class="buy">${cl.b}</span>/<span class="sell">${cl.s}</span>${cl.u ? '/<span class="unk">' + cl.u + '</span>' : ''}</span>` : '<span style="color:var(--faint)">·</span>'}</td>`;
      }
      h += '</tr>';
    }
    h += '</tbody></table></div>';
    $('#view').innerHTML =
      h +
      `<div class="key">Cell = contracts traded at that strike in 10 s (calls + puts), with <span class="buy">buy</span>/<span class="sell">sell</span>/<span class="unk">?</span> underneath. Shade ∝ √contracts. <b><u>Bold underlined</u></b> = contains a ticket ≥ $10k. <span style="color:var(--spot)">Yellow edge</span> = QQQ was just above this strike at the end of the slice.</div>`;
    $('#detail').innerHTML = 'Tap a cell to list every print in it.';
    $('#view')
      .querySelectorAll('td[data-r]')
      .forEach((td) => {
        td.tabIndex = 0;
        td.setAttribute('role', 'button');
        activate(td, () => {
          const cl = cells[+td.dataset.r][+td.dataset.k],
            t0 = +td.dataset.r * bin;
          $('#detail').innerHTML =
            `<div>${K[+td.dataset.k]} strike, ${clock(S, t0)}–${clock(S, t0 + bin)}: <b>${cl.ps.length}</b> prints (tickets first, then newest first)</div><div class="list">${cl.ps
              .slice()
              .sort((a, b) => (prem(b) >= BIG) - (prem(a) >= BIG) || b[0] - a[0])
              .map((p) => '<div>' + (prem(p) >= BIG ? '● ' : '') + printLine(S, p) + '</div>')
              .join('')}</div>`;
        });
      });
  }

  /* ---------- shared: strike crossings with a 5-cent buffer ---------- */
  function crossings(S) {
    const out = [];
    let last = null;
    for (const { t, spot: v } of S.points) {
      if (last && !S.gaps.some(([a, b]) => a <= t && b >= last.t)) {
        const a = Math.floor(last.spot),
          b = Math.floor(v);
        if (a !== b && Math.abs(v - Math.round(v)) >= 0.05) out.push({ t, k: Math.max(a, b), up: b > a, v });
        else if (a !== b) continue;
      }
      last = { t, spot: v };
    }
    return out;
  }

  /* ---------- D: mirrored ladder ---------- */
  let DCUR = null;
  function windowAgg(S, cur) {
    const agg = K.map(() => ({
      C: { hb: 0, hs: 0, hu: 0, ob: 0, os: 0, ou: 0, ps: [] },
      P: { hb: 0, hs: 0, hu: 0, ob: 0, os: 0, ou: 0, ps: [] },
    }));
    for (const p of S.prints) {
      if (p[0] > cur) break;
      if (p[0] < cur - D_WIN) continue;
      const a = agg[p[1]]?.[p[2]];
      if (!a) continue;
      const hot = cur - p[0] <= D_HOT ? 'h' : 'o';
      a[hot + p[5]] += prem(p);
      a.ps.push(p);
    }
    return agg;
  }
  function renderD(S) {
    const dur = S.duration;
    if (DCUR == null || DCUR > dur) DCUR = dur;
    let scale = 1;
    for (let c = D_WIN / 2; c <= dur; c += 5000) {
      for (const a of windowAgg(S, c))
        for (const t of ['C', 'P']) {
          const x = a[t];
          scale = Math.max(scale, x.hb + x.hs + x.hu + x.ob + x.os + x.ou);
        }
    }
    $('#view').innerHTML =
      `<div class="dctl"><span id="dnow"></span><input type="range" id="dslider" min="0" max="${dur}" step="1000" value="${DCUR}" aria-label="Move now through the scene"><button id="dlive">Back to end</button></div><div id="dban"></div><div class="dhead"><span>◀ calls · premium, last 60 s</span><span>strike</span><span>puts · premium, last 60 s ▶</span></div><div class="ladder" id="ladder"></div><div class="key">Bars: <span class="buy">buy</span> / <span class="sell">sell</span> / <span class="unk">side ?</span>; bright = last 10 s, dim = 10–60 s ago. The bar scale stays fixed while you scrub this window and recalculates when the window updates. Chip = largest ticket ≥ $10k in the window (+n = more). <span style="color:var(--spot)">━ QQQ</span> at "now". Tap a bar for its prints.</div>`;
    const ks = K.slice().reverse(),
      ROW = 44;
    const draw = () => {
      const cur = DCUR,
        agg = windowAgg(S, cur),
        v = spotAt(S, cur);
      renderNow({
        ...S,
        duration: cur,
        end: clock(S, cur),
        points: S.points.filter((p) => p.t <= cur),
        prints: S.prints.filter((p) => p[0] <= cur),
      });
      const detail = $('#detail');
      detail.textContent = 'Tap a bar for its prints.';
      $('#dnow').innerHTML = `now = <b style="color:var(--ink)">${clock(S, cur)} ET</b>`;
      const gap = S.gaps.find((g) => g[0] < cur && g[1] > cur - D_WIN);
      const g = gap && [Math.max(0, cur - D_WIN, gap[0]), Math.min(cur, gap[1])];
      $('#dban').innerHTML = g
        ? `<div class="dbanner"><b>Feed lost ${clock(S, g[0], true)} → ${clock(S, g[1], true)} (${((g[1] - g[0]) / 1000).toFixed(1)} s)</b> inside this window. These bars are missing those prints.</div>`
        : '';
      const seg = (x, k, op) =>
        x[k] > 0
          ? `<i style="flex:${x[k]};background:var(--${{ b: 'buy', s: 'sell', u: 'unk' }[k.slice(1)]});opacity:${op}"></i>`
          : '';
      let h = '';
      for (const k of ks) {
        const a = agg[K.indexOf(k)];
        const half = (t) => {
          const x = a[t],
            tot = x.hb + x.hs + x.hu + x.ob + x.os + x.ou;
          const big = x.ps.filter((p) => prem(p) >= BIG).sort((p, q) => prem(q) - prem(p));
          const chip = big.length
            ? `<span class="chip ${side[big[0][5]][1]}">${money(prem(big[0]))}${big.length > 1 ? ' +' + (big.length - 1) : ''}</span>`
            : '';
          const w = tot ? Math.max(1, (tot / scale) * 78) : 0;
          return `<div class="side-${t === 'C' ? 'l' : 'r'}" data-k="${k}" data-t="${t}" role="button" tabindex="0" aria-label="${k} ${t === 'C' ? 'calls' : 'puts'}, ${money(tot)} in the last 60 s"><span class="stack" style="width:${w}%">${seg(x, 'hb', 1)}${seg(x, 'hs', 1)}${seg(x, 'hu', 1)}${seg(x, 'ob', 0.35)}${seg(x, 'os', 0.35)}${seg(x, 'ou', 0.35)}</span>${tot ? `<span class="amt">${money(tot)}</span>` : ''}${chip}</div>`;
        };
        h += `<div class="lrow">${half('C')}<div class="k"><span>${k}</span></div>${half('P')}</div>`;
      }
      let line = '';
      if (v != null) {
        const lower = ks.findIndex((k) => k <= v);
        const lane =
          lower < 0
            ? ks.length - 0.5
            : v > ks[0]
              ? -0.5
              : lower === 0
                ? 0
                : lower - (v - ks[lower]) / (ks[lower - 1] - ks[lower]);
        const y = (lane + 0.5) * ROW;
        const off = v > ks[0] ? ' (above ladder)' : v < ks.at(-1) ? ' (below ladder)' : '';
        line = `<div class="spotline" style="top:${y}px"><span>QQQ ${v.toFixed(2)}${off}</span></div>`;
      } else line = `<div class="dbanner" style="margin-top:6px">QQQ not received at this moment.</div>`;
      $('#ladder').innerHTML = h + line;
      $('#ladder')
        .querySelectorAll('[data-k]')
        .forEach((el) => {
          const open = () => {
            const ps = agg[K.indexOf(+el.dataset.k)][el.dataset.t].ps.slice().reverse();
            $('#detail').innerHTML =
              `<div>${el.dataset.k} ${el.dataset.t === 'C' ? 'calls' : 'puts'}, ${clock(S, Math.max(0, cur - D_WIN))}–${clock(S, cur)}: <b>${ps.length}</b> prints, newest first</div><div class="list">${ps.map((p) => '<div>' + printLine(S, p) + '</div>').join('')}</div>`;
          };
          el.onclick = open;
          el.onkeydown = (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              open();
            }
          };
        });
    };
    $('#dslider').oninput = (e) => {
      DCUR = +e.target.value;
      draw();
    };
    $('#dlive').onclick = () => {
      DCUR = dur;
      $('#dslider').value = dur;
      draw();
    };
    $('#detail').innerHTML =
      'Tap a bar to list its prints. Drag the slider to replay; the ladder only ever shows prints up to "now".';
    draw();
  }

  /* ---------- E: event feed ---------- */
  function renderE(S) {
    const open = new Map(),
      events = [];
    for (const p of S.prints) {
      const key = p[6].symbol + ':' + p[5],
        g = open.get(key);
      if (
        g &&
        p[0] - g.ps[g.ps.length - 1][0] <= GROUP_MS &&
        p[0] - g.ps[0][0] <= GROUP_SPAN &&
        !S.gaps.some((x) => x[0] < p[0] && x[1] > g.ps[g.ps.length - 1][0])
      )
        g.ps.push(p);
      else {
        if (g) events.push(g);
        open.set(key, { ps: [p] });
      }
    }
    events.push(...open.values());
    const items = [],
      BG = 15000,
      bg = new Map();
    for (const e of events) {
      const v = e.ps.reduce((a, p) => a + prem(p), 0);
      if (v >= BIG) items.push({ k: 'ev', t: e.ps[0][0], e, v });
      else
        for (const p of e.ps) {
          const b = Math.min(Math.ceil(S.duration / BG) - 1, Math.floor(p[0] / BG));
          if (!bg.has(b)) bg.set(b, []);
          bg.get(b).push(p);
        }
    }
    for (const [b, ps] of bg) items.push({ k: 'bg', t: b * BG, ps, b });
    for (const c of crossings(S)) items.push({ k: 'cross', t: c.t, c });
    for (const g of S.gaps) items.push({ k: 'lost', t: g[0], g });
    let lastT = S.cappedFrom == null ? 0 : S.cappedFrom - S.t0;
    for (const p of S.prints) {
      const pause = p[0] - lastT;
      if (pause > 8000) {
        const g = S.gaps.find((g) => g[0] < p[0] && g[1] > lastT);
        const quietEnd = g ? g[0] : p[0];
        if (quietEnd - lastT > 3000) items.push({ k: 'quiet', t: lastT + 1, d: quietEnd - lastT });
      }
      lastT = p[0];
    }
    items.sort((a, b) => b.t - a.t);
    const verb = {
      b: 'Buyers lifted the offer on',
      s: 'Sellers hit the bid on',
      u: 'Trades with unknown side on',
    };
    let h = '';
    items.forEach((it, i) => {
      if (it.k === 'ev') {
        const ps = it.e.ps,
          p0 = ps[0],
          n = ps.length,
          q = ps.reduce((a, p) => a + p[4], 0);
        const span = (ps.at(-1)[0] - p0[0]) / 1000,
          sp = spotAt(S, p0[0]);
        const body =
          n === 1
            ? `one print of <b>${q.toLocaleString()}</b> contracts at $${p0[3].toFixed(2)}`
            : `<b>${n}</b> prints in ${span < 0.1 ? 'under 0.1' : span.toFixed(1)} s, <b>${q.toLocaleString()}</b> contracts`;
        h += `<div class="ev" data-i="${i}" role="button" tabindex="0"><span class="when">${clock(S, p0[0], true)}</span><span class="say"><span class="${side[p0[5]][1]}">${verb[p0[5]]}</span> the <b>${K[p0[1]]} ${p0[2] === 'C' ? 'calls' : 'puts'}</b>: ${body}, <b>${money(it.v)}</b>.<span class="meta">${sp != null ? 'QQQ ' + sp.toFixed(2) + ' last observed by the first print · ' + (K[p0[1]] - sp >= 0 ? '+' : '') + (K[p0[1]] - sp).toFixed(2) + ' from spot' : 'QQQ not received'}</span></span><div class="list" hidden></div></div>`;
      } else if (it.k === 'bg') {
        const ps = it.ps,
          v = ps.reduce((a, p) => a + prem(p), 0),
          q = ps.reduce((a, p) => a + p[4], 0);
        const n = { b: 0, s: 0, u: 0 };
        ps.forEach((p) => (n[p[5]] += p[4]));
        const ks = ps.map((p) => K[p[1]]);
        h += `<div class="ev small" data-i="${i}" role="button" tabindex="0"><span class="when">${clock(S, it.b * BG)}</span><span class="say">Background, ${clock(S, it.b * BG)}–${clock(S, Math.min((it.b + 1) * BG, S.duration))}: ${ps.length} smaller prints, ${q.toLocaleString()} contracts, ${money(v)}, strikes ${ks.reduce((a, b) => Math.min(a, b), Infinity)}–${ks.reduce((a, b) => Math.max(a, b), -Infinity)} · <span class="buy">${Math.round((100 * n.b) / q)}% buy</span> / <span class="sell">${Math.round((100 * n.s) / q)}% sell</span> / <span class="unk">${Math.round((100 * n.u) / q)}% ?</span></span><div class="list" hidden></div></div>`;
      } else if (it.k === 'cross')
        h += `<div class="ev xing"><span class="when">${clock(S, it.t)}</span><span class="say">QQQ crossed ${it.c.up ? 'above' : 'below'} the ${it.c.k} strike (${it.c.v.toFixed(2)}).</span></div>`;
      else if (it.k === 'lost')
        h += `<div class="ev lost"><span class="when">${clock(S, it.g[0], true)}</span><span class="say"><b>The feed was lost for ${((it.g[1] - it.g[0]) / 1000).toFixed(1)} s</b> (until ${clock(S, it.g[1], true)}). Prints in that time were not received; this is missing data, not a quiet market.</span></div>`;
      else
        h += `<div class="ev quiet"><span class="when">${clock(S, it.t)}</span><span class="say">No prints for ${(it.d / 1000).toFixed(1)} s in the displayed tape.</span></div>`;
    });
    $('#view').innerHTML =
      `<div class="feed">${h}</div><div class="key">Event = prints in the same contract and side, each within 1 s of the last and all within 3 s (a grouping rule, not an exchange label). Sentences are for events of $10k+; everything smaller is in the grey 15-second background lines. Verbs follow the inferred side. Tap a sentence for its prints.</div>`;
    $('#detail').innerHTML = '';
    $('#view')
      .querySelectorAll('.ev[data-i]')
      .forEach((el) => {
        const toggle = () => {
          const it = items[+el.dataset.i],
            box = el.querySelector('.list'),
            ps = (it.e ? it.e.ps : it.ps).slice().reverse();
          if (box.hidden && !box.innerHTML)
            box.innerHTML = ps.map((p) => '<div>' + printLine(S, p) + '</div>').join('');
          box.hidden = !box.hidden;
        };
        el.onclick = (e) => {
          if (!e.target.closest('.list')) toggle();
        };
        el.onkeydown = (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            toggle();
          }
        };
      });
  }

  function activate(el, fn) {
    el.onclick = fn;
    el.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        fn();
      }
    };
  }
  const renderers = {
    sales: ['A', renderA, 'Time & Sales'],
    chart: ['B', renderB, 'Price chart + tickets'],
    footprint: ['C', renderC, 'Strike footprint'],
    ladder: ['D', renderD, 'Mirrored ladder'],
    events: ['E', renderE, 'Event feed'],
  };
  const options = { classic: 'Current tape' };
  for (const [id, [letter, , name]] of Object.entries(renderers)) options[id] = letter + ' · ' + name;
  return {
    options,
    reset() {
      DCUR = null;
    },
    render(id, rows, spots, rawGaps, start, end, strikes, showAll, cappedFrom = null) {
      K = Array.from(new Set([...strikes, ...rows.map((p) => p.strike)]))
        .filter(Number.isFinite)
        .sort((a, b) => a - b);
      const indices = new Map(K.map((k, i) => [k, i]));
      const S = {
        t0: start,
        cappedFrom,
        duration: end - start,
        start: formatClock(start),
        end: formatClock(end),
        prints: rows.map((p) => [
          p.t - start,
          indices.get(p.strike),
          p.type,
          p.price,
          p.size,
          { buy: 'b', sell: 's' }[p.side] || 'u',
          p,
        ]),
        points: spots.map((p) => ({ t: p.t - start, spot: p.spot })),
        gaps: rawGaps
          .map((g) => [
            Math.max(0, (g.start ?? start) - start),
            Math.min(end - start, (g.end ?? end) - start),
          ])
          .filter(([a, b]) => b > a),
      };
      all = showAll;
      const [letter, draw] = renderers[id];
      $('#oh').textContent = OPT[letter][0];
      $('#op').textContent = OPT[letter][1];
      renderNow(S);
      $('#chart-strikes').hidden = id !== 'chart' || !K.length;
      if (!K.length) {
        $('#view').onscroll = null;
        $('#view').textContent = 'Waiting for strikes';
        $('#detail').textContent = '';
        return;
      }
      const viewport = $('#view');
      const sameView = viewport.dataset.view === id;
      const top = sameView ? viewport.scrollTop : 0;
      const left = sameView ? viewport.scrollLeft : 0;
      viewport.dataset.view = id;
      viewport.onscroll = null;
      draw(S);
      viewport.scrollTo(left, top);
      $('.panel').style.setProperty('--grid-width', 65 + K.length * 36 + 'px');
    },
  };
}
