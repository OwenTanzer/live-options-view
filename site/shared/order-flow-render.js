// Order Flow text rendering: the strike table rows and the freshness/coverage line.
// Pure string output so the rendered partial-coverage cases are testable in node.
// Classic script in the browser (window.OrderFlowRender) or CommonJS in tests.

(function (root) {
  const money = (v) => {
    if (v == null) return '—';
    const a = Math.abs(v), s = v < 0 ? '−' : v > 0 ? '+' : '';
    return s + '$' + (a >= 1e9 ? (a / 1e9).toFixed(2) + 'B' : a >= 1e6 ? (a / 1e6).toFixed(2) + 'M'
      : a >= 1e3 ? (a / 1e3).toFixed(1) + 'k' : a.toFixed(0));
  };
  const unsigned = (v) => (v == null ? '—' : money(v).replace(/^\+/, ''));
  const contractsCount = (v) => (v == null ? '—' : (v < 0 ? '−' : v > 0 ? '+' : '') + Math.abs(v).toLocaleString('en-US'));
  const gap = (text, title) => `<span class="gap"${title ? ` title="${title}"` : ''}>${text}</span>`;
  const partial = (c, what) => `${gap(`▨ ${c.covered}/${c.of}`, `${c.of - c.covered} of ${c.of} contracts lack ${what}`)}`;

  // Concentration with partial coverage shows its value and an explicit k/n marker. The
  // conditional total range is withheld unless every contract at the strike is covered,
  // because a strike range that silently omits a contract's ±OI bound is not a bound.
  function ladderRowsHtml(view, p, selected) {
    if (!view || !view.strikes.length) return '<tr><td colspan="6" class="muted">No covered contracts yet.</td></tr>';
    return view.strikes.map((s) => {
      const f = s.flow[p], t = s.total[p], c = s.concentration;
      const conc = c.value == null ? gap('no coverage')
        : unsigned(c.value) + (c.covered < c.of ? ' ' + partial(c, 'OI or gamma') : '');
      const flow = f.base == null ? gap('no coverage') : money(f.base) + (f.covered < f.of ? ' ' + partial(f, 'gamma or spot') : '');
      const total = t.low == null ? gap('no coverage')
        : t.covered < t.of ? gap(`withheld: ${t.covered}/${t.of} contracts covered`, 'Total range needs OI, gamma and flow for every contract at this strike')
          : money(t.low) + ' … ' + money(t.high);
      return `<tr tabindex="0" data-strike="${s.strike}" class="${selected === s.strike ? 'sel' : ''}">
            <td>${s.strike}</td>
            <td><span class="buy">${s.observed.B.toLocaleString()}</span> / <span class="sell">${s.observed.S.toLocaleString()}</span> / <span class="unknown">${s.observed.U.toLocaleString()}</span></td>
            <td>${conc}</td>
            <td>${flow}</td>
            <td>${f.base == null ? '—' : money(f.low) + ' … ' + money(f.high) + (f.uncertain ? ' · sign uncertain' : '')}</td>
            <td>${total}</td>
          </tr>`;
    }).join('');
  }

  function freshnessText(v, clock, extra = []) {
    if (!v) return '';
    const bits = [];
    if (v.snapshot) {
      bits.push('chain ' + clock(v.snapshot.t) + ' ET (' + Math.round(v.snapshot.age / 1000) + ' s old' +
        (v.snapshot.stale ? ', STALE → no gamma coverage' : '') + ')');
    } else bits.push('chain snapshot unavailable → no gamma coverage');
    const c = v.coverage;
    if (c && c.missingOpening) {
      bits.push('OPENING MISSING: no flow from ' + clock(c.missingOpening.start) + ' to ' +
        (c.missingOpening.end != null ? clock(c.missingOpening.end) : 'now') + ' ET');
    }
    if (c && c.from != null) bits.push('flow covered from ' + clock(c.from) + (c.to ? ' to ' + clock(c.to) : '') + ' ET');
    if (c && c.gaps && c.gaps.length) {
      bits.push(c.gaps.length + ' gap' + (c.gaps.length > 1 ? 's' : '') + ': ' +
        c.gaps.map((g) => (g.start != null ? clock(g.start) : '?') + '–' + (g.end != null ? clock(g.end) : 'open')).join(', '));
    }
    if (c && c.incomplete) bits.push('INCOMPLETE HISTORY');
    bits.push(...extra);
    if (v.clearingDate) bits.push('settled OI clearing date inferred ' + v.clearingDate);
    return bits.join(' · ');
  }

  const api = { money, unsigned, contractsCount, ladderRowsHtml, freshnessText };
  if (typeof module !== 'undefined') module.exports = api;
  else root.OrderFlowRender = api;
})(typeof window !== 'undefined' ? window : globalThis);
