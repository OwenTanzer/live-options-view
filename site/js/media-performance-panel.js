/* Media performance panel — render-only.
 *
 * This module never fetches, never validates a view key, and never
 * changes site/js/news-panel.js key validation, lease, or deadline
 * logic. It receives already-fetched, already-validated view data
 * (a view page with `performance` summaries attached by
 * market_data/news/performance_views.py) and renders it with
 * textContent only.
 *
 * Gate 4 tokens live in media-performance-panel.css. Red/green are
 * gain/loss only; blue/orange are story/horizon identity; intensity
 * is single-hue with monotonic lightness (see CSS).
 * Type floor: 12px for anything meant to be read.
 */

(function (global) {
  'use strict';

  // Pure helpers — testable in Node without a DOM.
  function formatReturn(returnPct) {
    if (returnPct === null || returnPct === undefined) return null;
    var sign = returnPct > 0 ? '+' : returnPct < 0 ? '\u2212' : '';
    return sign + Math.abs(returnPct).toFixed(2) + '%';
  }

  function formatBps(returnBps) {
    if (returnBps === null || returnBps === undefined) return null;
    var sign = returnBps > 0 ? '+' : returnBps < 0 ? '\u2212' : '';
    return sign + Math.abs(returnBps).toFixed(1) + ' bps';
  }

  function honestyLabel(row) {
    if (!row) return 'not yet available';
    if (row.hindsight || row.status === 'hindsight') return 'hindsight';
    if (row.status === 'matched') return row.base_stale ? 'stale base' : 'fresh';
    var reason = row.missing_reason || '';
    if (reason === 'not_yet_available') return 'pending';
    if (reason === 'no_series') return 'no coverage';
    if (reason.indexOf('unknown mapping') === 0 || !row.instrument_id) return 'unknown mapping';
    return 'not yet available';
  }

  function returnClass(returnPct, status) {
    if (status !== 'matched' || returnPct === null || returnPct === undefined) return 'mp-neutral';
    if (returnPct > 0) return 'mp-gain';
    if (returnPct < 0) return 'mp-loss';
    return 'mp-neutral';
  }

  function leadHorizon(summary) {
    // Lead with the 1h move when matched, else the largest available horizon.
    if (!summary || !summary.horizons) return null;
    var order = ['1h', '30m', '5m', '1d'];
    for (var i = 0; i < order.length; i++) {
      var h = summary.horizons[order[i]];
      if (h && h.status === 'matched') return h;
    }
    return summary.horizons['1h'] || summary.horizons['5m'] || null;
  }

  // DOM rendering — textContent only, no innerHTML.
  function el(doc, tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function renderHorizonChip(doc, horizon) {
    var chip = el(doc, 'span', 'mp-chip ' + returnClass(horizon.return_pct, horizon.status));
    var label = el(doc, 'span', 'mp-chip-horizon', horizon.horizon + (horizon.elapsed_seconds ? ' target · ' + horizon.elapsed_seconds / 60 + ' min elapsed' : ''));
    chip.appendChild(label);
    if (horizon.status === 'matched') {
      chip.appendChild(el(doc, 'span', 'mp-chip-value', formatReturn(horizon.return_pct)));
    } else {
      chip.appendChild(el(doc, 'span', 'mp-chip-value mp-missing', horizon.honesty_label));
    }
    chip.appendChild(el(doc, 'span', 'mp-chip-label', horizon.honesty_label));
    return chip;
  }

  function renderPerformanceCard(doc, row) {
    // Gate 3 (inspection): lead with the move, not the provenance.
    var card = el(doc, 'article', 'mp-card');
    var summary = row.performance || null;
    var lead = summary ? leadHorizon(summary) : null;

    var header = el(doc, 'header', 'mp-card-header');
    header.appendChild(el(doc, 'span', 'mp-ticker', row.instrument_id || 'Unmapped'));
    if (lead && lead.status === 'matched') {
      var move = el(doc, 'span', 'mp-move ' + returnClass(lead.return_pct, lead.status),
        formatReturn(lead.return_pct));
      header.appendChild(move);
      header.appendChild(el(doc, 'span', 'mp-move-horizon', lead.elapsed_seconds ? 'after ' + (lead.elapsed_seconds / 60) + ' min (' + lead.horizon + ' target)' : 'target ' + lead.horizon));
      header.appendChild(el(doc, 'span', 'mp-honesty', lead.honesty_label));
    } else if (lead) {
      header.appendChild(el(doc, 'span', 'mp-move mp-missing', lead.honesty_label));
    } else {
      header.appendChild(el(doc, 'span', 'mp-move mp-missing', 'performance not yet available'));
    }
    if (lead && lead.status === 'matched' && row.base_stale) {
      card.className += ' mp-stale';
    }
    card.appendChild(header);

    if (summary) {
      var chips = el(doc, 'div', 'mp-chips');
      ['5m', '30m', '1h', '1d'].forEach(function (name) {
        var h = summary.horizons[name];
        if (h) chips.appendChild(renderHorizonChip(doc, h));
      });
      card.appendChild(chips);
    }

    card.appendChild(el(doc, 'h3', 'mp-title', row.title || ''));
    var meta = el(doc, 'p', 'mp-meta', (row.outlet || '') + ' · ' + (row.event_at || ''));
    card.appendChild(meta);

    // Methodology & provenance: one interaction away, never a wall.
    var details = doc.createElement('details');
    details.className = 'mp-method';
    var summaryEl = doc.createElement('summary');
    summaryEl.textContent = 'Methodology & provenance';
    details.appendChild(summaryEl);
    var provenance = [
      'Event: ' + (row.event_at || '—') + ' (' + (row.event_basis || '—') + ')',
      'Published: ' + (row.published_at || '—') + ' · Observed: ' + (row.observed_at || '—'),
      'Mapping: ' + (row.instrument_mapping || 'unknown') +
        (row.matched_text ? ' (' + row.matched_text + ')' : ''),
      'Base: ' + (lead && lead.base_price !== undefined ? String(lead.base_price) : '—') +
        ' · Forward: ' + (lead && lead.forward_price_time ? lead.forward_price_time : '—'),
      'This view is rhetorical, not a reproduction of the tape: it selects ' +
        '5m / 30m / 1h / 1d and leads with the 1h move. Returns are calculated ' +
        'from observed prices; no price is interpolated and no missing horizon is shown as zero.',
      'Check: open the story and the forward price record — do they match the claim above?'
    ].join('\n');
    details.appendChild(el(doc, 'p', 'mp-provenance', provenance));
    card.appendChild(details);
    return card;
  }

  function renderMediaPerformance(doc, container, viewPage) {
    // container is already in the page; this function only fills it.
    if (!container || !viewPage || !Array.isArray(viewPage.rows)) return 0;
    var count = 0;
    viewPage.rows.forEach(function (row) {
      container.appendChild(renderPerformanceCard(doc, row));
      count += 1;
    });
    return count;
  }

  var api = {
    formatReturn: formatReturn,
    formatBps: formatBps,
    honestyLabel: honestyLabel,
    returnClass: returnClass,
    leadHorizon: leadHorizon,
    renderPerformanceCard: renderPerformanceCard,
    renderMediaPerformance: renderMediaPerformance
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (global) {
    global.MediaPerformance = api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
