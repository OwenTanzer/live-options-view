// NASDAQ-100 Stocks panel — read-only display of the NASDAQ-100 snapshot
// published by market_data/nasdaq_collector/ (a parallel workstream; see the
// artifact contract note below). Classic script, loaded by index.html between
// news-panel.js and tab-switching.js; the Stocks tab mounts it once, on first
// open (tab-switching.js calls loadNasdaqPanel), like the News panel.
// Plain JS, no framework, no user state, no trading, no alerts, no favourites.
// Text from the artifact goes through escapeHtml / textContent-safe string
// building only; news links are re-checked with new URL() at render time.
// Every number is shown with the snapshot's as-of time and source; a snapshot
// older than STALE_AFTER_MS, or one that cannot be fetched, is shown as an
// explicit stale / source-unavailable state, never as fresh data.
//
// ── Artifact contract — docs/specs/NASDAQ_SNAPSHOT.md (collector wins) ──
// GET nasdaq/latest.json (under BROWSER_DATA, the same public-data origin the
// squeeze panel reads from):
// {
//   "as_of": "2026-11-23T21:00:00Z", "schema_version": 1, "dataset": "nasdaq_snapshot",
//   "universe": { "source": "Nasdaq official list-type/nasdaq100 API",
//     "listings": 101, "companies": 100, "membership_source": "live" },
//   "index_changes": { "as_of": "2026-11-23",
//     "added": [{ "symbol": "NEW1", "name": "New Entrant Inc." }],
//     "removed": [{ "symbol": "OLD1", "name": "Old Leaver Inc." }],
//     "source": "live", "fallback": false },
//   "index": { "symbol": "NDX", "status": "available", "level": 25431.10,
//     "percent_change": 0.42, "percent_change_basis": "regular-session move vs previous close",
//     "previous_close": 25324.50, "day_low": 25210.00, "day_high": 25480.00,
//     "market_status": { "status": "closed", "reason": "outside_hours" },
//     "breadth": { "advancers": 62, "decliners": 38, "unchanged": 1 },
//     "top_gainers": [{ "symbol": "NVDA", "percent_change": 1.83 }],
//     "top_losers": [{ "symbol": "MSFT", "percent_change": -0.5 }] },
//   "stocks": [                                  // the 101 NASDAQ-100 listings
//     { "symbol": "NVDA", "name": "NVIDIA Corp", "price": 178.42,
//       "percent_change": 1.83,
//       "percent_change_basis": "Nasdaq latest reported move (after-hours move after the close)",
//       "market_cap": 4350000000000, "rank": 1,
//       "day_high": 180.10, "day_low": 176.20, "volume": 182340000,
//       "news": [{ "title": "...", "source": "Reuters",
//                   "url": "https://…", "published_at": "2026-11-23T18:12:00Z" }] }
//   ],
//   "coverage": { "listings": 101, "available": 101, "source_unavailable": 0 }
// }
// Candles are NOT in the snapshot (it would be megabytes hourly). The panel
// fetches them on demand, per selected stock, from Yahoo Finance's public
// chart API — the same source the tracker uses — and aggregates the
// non-standard intervals (3m, 9m, 2h, 4h) from real 1-minute bars on
// America/New_York time. A failed candle fetch is an explicit "Candles
// unavailable" state, never a substituted chart.
// Honesty labelling (mandatory, and stated on the page under the table):
// the index % is "vs prev close (regular session)"; each stock's % is the
// "Nasdaq latest reported move". The two bases are different and must never
// be compared directly. Sources: prices from Nasdaq's official API, candles
// from Yahoo Finance, headlines from Google News per ticker (as the
// NASDAQ-100 Stock Tracker records them).
// Membership is dynamic: the collector re-fetches Nasdaq's live
// constituent list every run, so the snapshot's stock rows follow index
// entrants and leavers; additions/removals are recorded in
// `index_changes` (with the run date) and shown under the controls. A
// leaver has no live row but its symbol and name stay in `index_changes`.
// Views: the Heatmap is the default view on open, with a Heatmap/Table
// toggle in the controls. Heatmap tiles are a squarified treemap of the
// same live snapshot rows as the table (so it follows dynamic membership
// and updates on every refresh), sized by market cap with SQUARE-ROOT
// scaling and a minimum-size floor (readability and tappability over
// extreme fidelity to market-cap proportionality), coloured green/red by
// daily % change with intensity by magnitude (full at +/-3%), each tile
// labelled ticker + % change with a -3%/+3% gradient legend. Tapping a
// tile opens the same stock detail view (candle chart + news) as tapping
// a table row.
(function (root) {
  'use strict';

  var ARTIFACT_PATH = 'nasdaq/latest.json';
  // The tracker refreshes hourly; 90 minutes gives one missed cycle of slack
  // before the panel calls the snapshot stale.
  var STALE_AFTER_MS = 90 * 60 * 1000;
  var INTERVALS = ['1m', '3m', '5m', '9m', '15m', '30m', '1h', '2h', '4h', '1d', '1w'];
  var INDEX_PCT_LABEL = 'vs prev close (regular session)';
  var STOCK_PCT_LABEL = 'Nasdaq latest reported move';
  var SOURCE_LINE = 'Prices: Nasdaq official API · Candles: Yahoo Finance · Headlines: Google News';
  var YAHOO_CHART = 'https://query1.finance.yahoo.com/v8/finance/chart/';
  var INTERVAL_MINUTES = { '1m': 1, '3m': 3, '5m': 5, '9m': 9, '15m': 15, '30m': 30,
    '1h': 60, '2h': 120, '4h': 240, '1d': 1440, '1w': 10080 };
  // Heatmap: tile colour reaches full green/red at +/-3% daily change.
  var HEATMAP_FULL_PCT = 3;
  // Heatmap: every tile gets at least this share of the total treemap area
  // (0.5%), so the smallest listings stay readable and tappable. Visibility
  // and ease of access win over extreme fidelity to market-cap proportions.
  var HEATMAP_MIN_SHARE = 0.005;
  // Treemap coordinate space (percentages are derived from it in
  // renderHeatmap); the container's aspect-ratio CSS matches it.
  var HEATMAP_W = 1000, HEATMAP_H = 620;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // A news link is rendered only if it is already a plain http(s) URL with an
  // authority and no userinfo — the same rule the News panel applies.
  function safeUrl(value) {
    if (typeof value !== 'string' || value === '' || value.length > 2048) return null;
    if (value.indexOf('\\') !== -1 || !/^https?:\/\/[^/?#]/i.test(value)) return null;
    var u;
    try { u = new URL(value); } catch (e) { return null; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    if (!u.hostname || u.username || u.password) return null;
    var authority = value.replace(/^[a-z]+:\/\//i, '').split(/[/?#]/)[0];
    if (authority.indexOf('@') !== -1) return null;
    return u.href;
  }

  function isFiniteNumber(v) { return typeof v === 'number' && Number.isFinite(v); }
  function isObj(x) { return x !== null && typeof x === 'object' && !Array.isArray(x); }

  function fmtPrice(v) {
    if (!isFiniteNumber(v)) return '—';
    return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtPct(v) {
    if (!isFiniteNumber(v)) return '—';
    return (v > 0 ? '+' : '') + v.toFixed(2) + '%';
  }
  function fmtInt(v) {
    if (!isFiniteNumber(v)) return '—';
    return Math.round(v).toLocaleString('en-US');
  }
  function fmtMarketCap(v) {
    if (!isFiniteNumber(v)) return '—';
    if (v >= 1e12) return '$' + (v / 1e12).toFixed(2) + 'T';
    if (v >= 1e9) return '$' + (v / 1e9).toFixed(1) + 'B';
    if (v >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
    return '$' + fmtInt(v);
  }
  function fmtTime(iso) {
    if (typeof iso !== 'string' || !iso) return 'unknown time';
    var t = Date.parse(iso);
    if (!Number.isFinite(t)) return 'unknown time';
    return new Date(t).toISOString().replace('T', ' ').replace(/(\.\d+)?Z$/, ' UTC');
  }
  function pctClass(v) {
    if (!isFiniteNumber(v) || v === 0) return 'flat';
    return v > 0 ? 'up' : 'down';
  }

  // Collector shape: index.market_status is {status, reason}; a plain
  // string is accepted as a fallback. Never renders "[object Object]".
  function marketStatusText(idx) {
    var ms = idx && idx.market_status;
    if (typeof ms === 'string') return ms;
    if (isObj(ms) && typeof ms.status === 'string') {
      return ms.reason ? ms.status + ' (' + ms.reason + ')' : ms.status;
    }
    return 'unknown';
  }

  // A snapshot is stale once it is older than STALE_AFTER_MS; a missing or
  // unparseable as_of is stale too — its age cannot be established, so it
  // must not be presented as fresh.
  function isStale(asOf, nowMs) {
    var t = typeof asOf === 'string' ? Date.parse(asOf) : NaN;
    if (!Number.isFinite(t)) return true;
    return (nowMs || Date.now()) - t > STALE_AFTER_MS;
  }

  // The structure the renderer relies on, checked before anything renders.
  // A snapshot that fails is a visible failure, never a blank or half table.
  function validateSnapshot(data) {
    var problems = [];
    if (!isObj(data)) return ['not an object'];
    if (typeof data.as_of !== 'string' || !Number.isFinite(Date.parse(data.as_of))) {
      problems.push('as_of is missing or not a valid time');
    }
    if (!isObj(data.index)) problems.push('index is missing');
    else {
      // A failed NDX fetch leaves index.level null; the index hero then
      // shows '—' and the stock rows still stand on their own (per the
      // contract). Only a present non-number is a problem.
      if (data.index.level !== undefined && data.index.level !== null &&
          !isFiniteNumber(data.index.level)) problems.push('index.level is not a number');
      if (data.index.percent_change !== undefined && data.index.percent_change !== null &&
          !isFiniteNumber(data.index.percent_change)) problems.push('index.percent_change is not a number');
    }
    if (!Array.isArray(data.stocks)) problems.push('stocks is not a list');
    else data.stocks.forEach(function (s, i) {
      if (!isObj(s)) { problems.push('stocks[' + i + '] is not an object'); return; }
      if (typeof s.symbol !== 'string' || !s.symbol) problems.push('stocks[' + i + '].symbol is missing');
      ['price', 'percent_change', 'market_cap'].forEach(function (k) {
        if (s[k] !== undefined && s[k] !== null && !isFiniteNumber(s[k])) {
          problems.push('stocks[' + i + '].' + k + ' is not a number');
        }
      });
      if (s.news !== undefined && !Array.isArray(s.news)) problems.push('stocks[' + i + '].news is not a list');
    });
    if (data.candles !== undefined && !isObj(data.candles)) problems.push('candles is not an object');
    return problems;
  }

  function sortStocks(stocks, key, dir) {
    var d = dir === -1 ? -1 : 1;
    var copy = (stocks || []).slice();
    copy.sort(function (a, b) {
      var av = a[key], bv = b[key];
      // Missing values sort last in either direction: a missing price is not
      // a zero, and must never outrank a real one.
      var aMissing = av === undefined || av === null;
      var bMissing = bv === undefined || bv === null;
      if (aMissing && bMissing) return 0;
      if (aMissing) return 1;
      if (bMissing) return -1;
      if (key === 'name') {
        var an = String(a.name || a.symbol), bn = String(b.name || b.symbol);
        return d * an.localeCompare(bn);
      }
      return d * (av - bv);
    });
    return copy;
  }

  function searchStocks(stocks, query) {
    var q = typeof query === 'string' ? query.trim().toLowerCase() : '';
    if (!q) return (stocks || []).slice();
    return (stocks || []).filter(function (s) {
      return String(s.symbol || '').toLowerCase().indexOf(q) !== -1 ||
        String(s.name || '').toLowerCase().indexOf(q) !== -1;
    });
  }

  function topMovers(stocks, n, dir) {
    var ranked = (stocks || []).filter(function (s) { return isFiniteNumber(s.percent_change); });
    ranked.sort(function (a, b) { return dir * (a.percent_change - b.percent_change); });
    return ranked.slice(0, n);
  }

  function breadth(index, stocks) {
    // Collector shape: index.breadth = {advancers, decliners, unchanged}.
    // Flat advancers/decliners on the index block are accepted as a fallback.
    var bObj = index && isObj(index.breadth) ? index.breadth : null;
    var adv = bObj && isFiniteNumber(bObj.advancers) ? bObj.advancers :
      (isFiniteNumber(index && index.advancers) ? index.advancers : null);
    var dec = bObj && isFiniteNumber(bObj.decliners) ? bObj.decliners :
      (isFiniteNumber(index && index.decliners) ? index.decliners : null);
    if (adv === null || dec === null) {
      adv = 0; dec = 0;
      (stocks || []).forEach(function (s) {
        if (isFiniteNumber(s.percent_change)) {
          if (s.percent_change > 0) adv++;
          else if (s.percent_change < 0) dec++;
        }
      });
    }
    return { advancers: adv, decliners: dec };
  }

  function moverItem(s) {
    return '<li class="nq-mover"><span class="nq-mover-sym">' + escapeHtml(s.symbol) + '</span>' +
      '<span class="nq-mover-name">' + escapeHtml(s.name || '') + '</span>' +
      '<span class="nq-mover-price">' + fmtPrice(s.price) + '</span>' +
      '<span class="nq-mover-change ' + pctClass(s.percent_change) + '">' + fmtPct(s.percent_change) + '</span></li>';
  }

  function renderHero(data, stale) {
    var idx = data.index || {};
    var b = breadth(idx, data.stocks);
    var total = b.advancers + b.decliners;
    var advPct = total ? Math.round((b.advancers / total) * 100) : 0;
    return '<div class="nq-hero">' +
      '<div class="nq-hero-main"><span class="nq-hero-label">NASDAQ-100 (NDX)</span>' +
      '<span class="nq-hero-level">' + fmtPrice(idx.level) + '</span>' +
      '<span class="nq-hero-change ' + pctClass(idx.percent_change) + '">' + fmtPct(idx.percent_change) + '</span>' +
      '<span class="nq-hero-basis">Index %: ' + escapeHtml(INDEX_PCT_LABEL) + '</span></div>' +
      '<div class="nq-hero-meta">' +
      '<span>Market at hourly snapshot: ' + escapeHtml(marketStatusText(idx)) + '</span>' +
      '<span>Prev close: ' + fmtPrice(isFiniteNumber(idx.previous_close) ? idx.previous_close : idx.prev_close) + '</span>' +
      '<span>Day range: ' + fmtPrice(idx.day_low) + ' – ' + fmtPrice(idx.day_high) + '</span>' +
      '<span>As of ' + escapeHtml(fmtTime(data.as_of)) + '</span>' +
      '<span>' + escapeHtml(SOURCE_LINE) + '</span>' +
      (stale ? '<span class="nq-stale-badge">STALE — older than ' + Math.round(STALE_AFTER_MS / 60000) + ' min</span>' : '') +
      '</div></div>' +
      '<div class="nq-breadth" role="img" aria-label="Breadth: ' + b.advancers + ' advancers, ' + b.decliners + ' decliners">' +
      '<span class="nq-breadth-bar"><span class="nq-breadth-adv" style="width:' + advPct + '%"></span></span>' +
      '<span>' + b.advancers + ' advancers · ' + b.decliners + ' decliners</span></div>';
  }

  function renderMovers(stocks) {
    var gainers = topMovers(stocks, 5, -1);
    var losers = topMovers(stocks, 5, 1);
    if (!gainers.length && !losers.length) return '<p class="nq-empty">No movers in this snapshot.</p>';
    return '<div class="nq-movers-grid"><section><h3>Top gainers</h3><ol class="nq-mover-list">' +
      gainers.map(moverItem).join('') + '</ol></section>' +
      '<section><h3>Top losers</h3><ol class="nq-mover-list">' +
      losers.map(moverItem).join('') + '</ol></section></div>';
  }

  function renderRows(stocks) {
    if (!stocks.length) {
      return '<tr><td colspan="6" class="nq-empty">No stocks match.</td></tr>';
    }
    return stocks.map(function (s) {
      var newsCount = isFiniteNumber(s.news_count) ? s.news_count :
        (Array.isArray(s.news) ? s.news.length : 0);
      return '<tr data-symbol="' + escapeHtml(s.symbol) + '">' +
        '<td class="nq-num">' + (isFiniteNumber(s.rank) ? s.rank : '—') + '</td>' +
        '<td class="nq-sym"><strong>' + escapeHtml(s.symbol) + '</strong>' +
        '<span class="nq-name">' + escapeHtml(s.name || '') + '</span></td>' +
        '<td class="nq-num">' + fmtPrice(s.price) + (s.minuteLabel ? '<small> ' + escapeHtml(s.minuteLabel) + '</small>' : '') + '</td>' +
        '<td class="nq-num ' + pctClass(s.percent_change) + '">' + fmtPct(s.percent_change) + '</td>' +
        '<td class="nq-num">' + fmtMarketCap(s.market_cap) + '</td>' +
        '<td class="nq-num">' + newsCount + '</td></tr>';
    }).join('');
  }

  // ── Heatmap (squarified treemap of the same snapshot rows) ──────────

  // Tile colour: green/red by daily % change, intensity by magnitude,
  // full at +/-HEATMAP_FULL_PCT. Interpolated from a dark neutral so a
  // flat stock is a visible tile, not a hole; a missing % is neutral.
  function heatmapColor(pct) {
    var neutral = [42, 47, 55];
    var up = [0, 204, 85], down = [238, 51, 0];
    if (!isFiniteNumber(pct) || pct === 0) return 'rgb(42, 47, 55)';
    var t = Math.min(Math.abs(pct), HEATMAP_FULL_PCT) / HEATMAP_FULL_PCT;
    var base = pct > 0 ? up : down;
    var rgb = [0, 1, 2].map(function (i) {
      return Math.round(neutral[i] + (base[i] - neutral[i]) * t);
    });
    return 'rgb(' + rgb.join(', ') + ')';
  }

  // Raise any weight below minShare of the total until every share is at
  // least minShare (bounded iteration; converges, since each pass raises
  // the small weights toward a fixed point). Returns normalised shares
  // summing to 1. A missing market cap is replaced by the median weight
  // by the caller, so it never becomes a zero tile.
  function applyMinShare(weights, minShare) {
    var total = 0;
    weights.forEach(function (v) { total += v; });
    if (!weights.length || total <= 0) {
      return weights.map(function () { return 0; });
    }
    var w = weights.slice();
    for (var i = 0; i < 10; i++) {
      var sum = 0;
      w.forEach(function (v) { sum += v; });
      var floor = minShare * sum;
      var changed = false;
      w = w.map(function (v) { if (v < floor) { changed = true; return floor; } return v; });
      if (!changed) break;
    }
    var sum2 = 0;
    w.forEach(function (v) { sum2 += v; });
    return w.map(function (v) { return v / sum2; });
  }

  // Squarified treemap layout (Bruls et al.): items [{key, weight}] with
  // weight > 0; returns [{key, x, y, w, h}] in the same units as
  // width/height, tiling the rectangle exactly. Rows are laid out along
  // the shorter side of the remaining rectangle and closed when adding
  // the next item would make the row's rectangles less square. Largest
  // first (stable for ties), so the biggest listings land first.
  function layoutTreemap(items, width, height) {
    var list = (items || []).filter(function (it) {
      return it && isFiniteNumber(it.weight) && it.weight > 0;
    });
    var total = 0;
    list.forEach(function (it) { total += it.weight; });
    if (!list.length || total <= 0 || width <= 0 || height <= 0) return [];
    var sorted = list.slice().sort(function (a, b) { return b.weight - a.weight; });
    var scale = (width * height) / total;
    var rects = [];
    var x = 0, y = 0, w = width, h = height;
    var row = [], rowSum = 0;
    function worst(rowItems, sum, side) {
      var max = 0, min = Infinity;
      rowItems.forEach(function (it) {
        var a = it.weight * scale;
        if (a > max) max = a;
        if (a < min) min = a;
      });
      var rowArea = sum * scale;
      return Math.max((side * side * max) / (rowArea * rowArea),
        (rowArea * rowArea) / (side * side * min));
    }
    function layoutRow(rowItems, sum) {
      var rowArea = sum * scale;
      if (w >= h) {
        var rh = rowArea / w;
        var cx = x;
        rowItems.forEach(function (it) {
          var cw = (it.weight * scale) / rh;
          rects.push({ key: it.key, x: cx, y: y, w: cw, h: rh });
          cx += cw;
        });
        y += rh; h = Math.max(0, h - rh);
      } else {
        var cw2 = rowArea / h;
        var cy = y;
        rowItems.forEach(function (it) {
          var ch = (it.weight * scale) / cw2;
          rects.push({ key: it.key, x: x, y: cy, w: cw2, h: ch });
          cy += ch;
        });
        x += cw2; w = Math.max(0, w - cw2);
      }
    }
    sorted.forEach(function (it) {
      var side = Math.min(w, h);
      if (row.length && side > 0 &&
          worst(row.concat([it]), rowSum + it.weight, side) > worst(row, rowSum, side)) {
        layoutRow(row, rowSum);
        row = [it]; rowSum = it.weight;
      } else {
        row.push(it); rowSum += it.weight;
      }
    });
    if (row.length) layoutRow(row, rowSum);
    return rects;
  }

  function renderHeatmap(stocks) {
    var list = (stocks || []).slice();
    if (!list.length) return '<p class="nq-empty">No stocks to show in the heatmap.</p>';
    // Largest market cap first: the squarified layout places the biggest
    // tiles first. Tile area is proportional to the SQUARE ROOT of market
    // cap (a $4T listing is ~66x a $1B one by cap, ~8x by square root),
    // with a minimum-share floor so small caps stay readable and tappable.
    var ordered = sortStocks(list, 'market_cap', -1);
    var roots = ordered.map(function (s) {
      return isFiniteNumber(s.market_cap) && s.market_cap > 0 ? Math.sqrt(s.market_cap) : null;
    });
    var present = roots.filter(function (v) { return v !== null; }).sort(function (a, b) { return a - b; });
    var median = present.length ? present[Math.floor(present.length / 2)] : 1;
    var raw = roots.map(function (v) { return v === null ? median : v; });
    var shares = applyMinShare(raw, HEATMAP_MIN_SHARE);
    var rects = layoutTreemap(ordered.map(function (s, i) {
      return { key: s.symbol, weight: shares[i] };
    }), HEATMAP_W, HEATMAP_H);
    var bySymbol = {};
    rects.forEach(function (r) { bySymbol[r.key] = r; });
    var tiles = ordered.map(function (s) {
      var r = bySymbol[s.symbol];
      if (!r) return '';
      return '<button type="button" class="nq-tile" data-symbol="' + escapeHtml(s.symbol) + '"' +
        ' style="left:' + (r.x / HEATMAP_W * 100).toFixed(2) + '%;top:' + (r.y / HEATMAP_H * 100).toFixed(2) +
        '%;width:' + (r.w / HEATMAP_W * 100).toFixed(2) + '%;height:' + (r.h / HEATMAP_H * 100).toFixed(2) +
        '%;background:' + heatmapColor(s.percent_change) + '"' +
        ' title="' + escapeHtml(s.symbol) + ' ' + fmtPct(s.percent_change) +
        ' · ' + fmtMarketCap(s.market_cap) + ' · ' + fmtPrice(s.price) + ' ' + escapeHtml(s.minuteLabel || 'hourly') + '">' +
        '<span class="nq-tile-sym">' + escapeHtml(s.symbol) + '</span>' +
        '<span class="nq-tile-pct">' + fmtPct(s.percent_change) + '</span></button>';
    }).join('');
    return '<div class="nq-heatmap" role="group" aria-label="NASDAQ-100 heatmap: tiles sized by market cap, coloured by daily percent change">' +
      tiles + '</div>' +
      '<div class="nq-legend" role="img" aria-label="Heatmap legend: full red at minus 3 percent, full green at plus 3 percent">' +
      '<span>-3%</span><span class="nq-legend-bar"></span><span>+3%</span></div>';
  }

  // The snapshot's index_changes section, as one line under the controls:
  // additions/removals with the run date, a "no changes" line when the
  // live list matches the embedded one, and an explicit fallback line
  // when the live list was unavailable (additions/removals unknown).
  // A leaver's symbol and name are preserved here even though it has no
  // live row. `membership_changes` is accepted as an alias, same shape.
  function renderIndexChanges(data) {
    var changes = (data && (data.index_changes || data.membership_changes)) || null;
    if (!changes) return '';
    var added = Array.isArray(changes.added) ? changes.added : [];
    var removed = Array.isArray(changes.removed) ? changes.removed : [];
    var when = (typeof changes.as_of === 'string' && changes.as_of)
      ? ' as of ' + escapeHtml(changes.as_of) : '';
    function names(list) {
      return list.map(function (c) {
        var sym = (typeof c === 'string') ? c : (c && c.symbol);
        var nm = (c && typeof c === 'object' && c.name) ? ' (' + c.name + ')' : '';
        return escapeHtml(sym || '') + escapeHtml(nm);
      }).join(', ');
    }
    if (changes.fallback) {
      return '<p class="nq-changes">Index membership' + when + ': the live constituent list was ' +
        'unavailable on this run, so the embedded universe is shown and additions/removals are unknown.</p>';
    }
    if (!added.length && !removed.length) {
      return '<p class="nq-changes">Index membership' + when + ': no changes.</p>';
    }
    var parts = [];
    if (added.length) parts.push('Added: ' + names(added));
    if (removed.length) parts.push('Removed: ' + names(removed));
    return '<p class="nq-changes">Index changes' + when + ': ' + parts.join('; ') + '.</p>';
  }

  // Candlesticks as inline SVG (numbers only, so nothing from the artifact
  // can inject markup). An interval with no candles is an explicit
  // "candles unavailable" state, never an empty chart that reads as flat.
  function renderCandles(symbol, interval, candles) {
    var list = candles && (candles[symbol + '|' + interval] || (candles[symbol] && candles[symbol][interval]));
    if (list === 'loading') {
      return '<p class="nq-empty" role="status">Loading candles for ' + escapeHtml(symbol) +
        ' at the ' + escapeHtml(interval) + ' interval…</p>';
    }
    if (!Array.isArray(list) || !list.length) {
      return '<p class="nq-empty" role="status">Candles unavailable for ' + escapeHtml(symbol) +
        ' at the ' + escapeHtml(interval) + ' interval — no candles published for this interval.</p>';
    }
    var W = 640, H = 200, PAD = 8, VOL_H = 36;
    var chartH = H - VOL_H - PAD;
    var lows = list.map(function (c) { return c.low; }).filter(isFiniteNumber);
    var highs = list.map(function (c) { return c.high; }).filter(isFiniteNumber);
    var vols = list.map(function (c) { return c.volume; }).filter(isFiniteNumber);
    if (!lows.length || !highs.length) {
      return '<p class="nq-empty" role="status">Candles unavailable for ' + escapeHtml(symbol) +
        ' at the ' + escapeHtml(interval) + ' interval — published candles have no usable prices.</p>';
    }
    var lo = Math.min.apply(null, lows), hi = Math.max.apply(null, highs);
    if (hi === lo) { hi = lo + 1; }
    var maxVol = vols.length ? Math.max.apply(null, vols) : 0;
    var slot = W / list.length;
    var bodyW = Math.max(2, Math.min(14, slot * 0.6));
    function y(price) { return PAD + (1 - (price - lo) / (hi - lo)) * (chartH - PAD); }
    var parts = [];
    list.forEach(function (c, i) {
      if (![c.open, c.high, c.low, c.close].every(isFiniteNumber)) return;
      var cx = slot * i + slot / 2;
      var up = c.close >= c.open;
      var colour = up ? '#00cc55' : '#ee3300';
      var top = y(Math.max(c.open, c.close));
      var height = Math.max(1, Math.abs(y(c.open) - y(c.close)));
      parts.push('<line x1="' + cx.toFixed(1) + '" y1="' + y(c.high).toFixed(1) +
        '" x2="' + cx.toFixed(1) + '" y2="' + y(c.low).toFixed(1) + '" stroke="' + colour + '" stroke-width="1"/>');
      parts.push('<rect x="' + (cx - bodyW / 2).toFixed(1) + '" y="' + top.toFixed(1) +
        '" width="' + bodyW.toFixed(1) + '" height="' + height.toFixed(1) + '" fill="' + colour + '"/>');
      if (isFiniteNumber(c.volume) && maxVol > 0) {
        var vh = (c.volume / maxVol) * (VOL_H - 4);
        parts.push('<rect x="' + (cx - bodyW / 2).toFixed(1) + '" y="' + (H - vh).toFixed(1) +
          '" width="' + bodyW.toFixed(1) + '" height="' + vh.toFixed(1) + '" fill="' + colour + '" opacity="0.35"/>');
      }
    });
    var first = list[0], last = list[list.length - 1];
    return '<svg class="nq-candles" viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="img" ' +
      'aria-label="Candlestick chart for ' + escapeHtml(symbol) + ' at the ' + escapeHtml(interval) +
      ' interval, ' + list.length + ' candles, range ' + fmtPrice(lo) + ' to ' + fmtPrice(hi) + '">' +
      parts.join('') + '</svg>' +
      '<p class="nq-chart-meta">Range ' + fmtPrice(lo) + ' – ' + fmtPrice(hi) +
      ' · ' + list.length + ' candles · first ' + escapeHtml(fmtTime(first.time)) +
      ' · last ' + escapeHtml(fmtTime(last.time)) + (last.source ? ' · ' + escapeHtml(last.source) : '') + '</p>';
  }

  // Aggregate 1-minute bars into a coarser intraday interval. Bars are
  // grouped in runs of `minutes` starting at each session day's first bar
  // (the collector buckets from the 09:30 ET open the same way); a partial
  // trailing group is kept — it is the live, still-forming bar. Only
  // regular-session bars are aggregated; pre/post-market bars are dropped
  // by the caller (Yahoo 1m bars are regular-session only by default).
  // Returns bars in the same {open, high, low, close, volume, time} shape.
  function aggregateBars(bars1m, minutes) {
    if (!Array.isArray(bars1m) || !bars1m.length || minutes <= 1) {
      return (bars1m || []).slice();
    }
    var out = [];
    var cur = null;
    var bucketKey = null;
    var formatter = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York',
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    bars1m.slice().sort(function (left, right) { return Date.parse(left.time) - Date.parse(right.time); }).forEach(function (b) {
      if (!b || ![b.open, b.high, b.low, b.close].every(isFiniteNumber)) return;
      var t = Date.parse(b.time);
      if (!Number.isFinite(t)) return;
      var parts = Object.fromEntries(formatter.formatToParts(t).map(function (part) { return [part.type, part.value]; }));
      var sessionMinute = Number(parts.hour) * 60 + Number(parts.minute) - 570;
      if (sessionMinute < 0 || sessionMinute >= 390) return;
      var bucket = Math.floor(sessionMinute / minutes);
      var key = parts.year + '-' + parts.month + '-' + parts.day + '|' + bucket;
      if (!cur || key !== bucketKey) {
        if (cur) out.push(cur);
        cur = { open: b.open, high: b.high, low: b.low, close: b.close,
          volume: isFiniteNumber(b.volume) ? b.volume : 0,
          time: new Date(t - (sessionMinute - bucket * minutes) * 60000).toISOString() };
        bucketKey = key;
      } else {
        cur.high = Math.max(cur.high, b.high);
        cur.low = Math.min(cur.low, b.low);
        cur.close = b.close;
        cur.volume += isFiniteNumber(b.volume) ? b.volume : 0;
      }
    });
    if (cur) out.push(cur);
    return out;
  }

  // Fetch candle bars for one symbol + interval from Yahoo Finance's public
  // chart API (no key). Intraday intervals come from real 1-minute bars
  // (range 5d) aggregated by aggregateBars; 1d uses daily bars (range 1y)
  // and 1w weekly bars (range 5y). Returns a Promise of bars, or [] on any
  // failure — a failed fetch is an explicit "Candles unavailable" state.
  function yahooBars(symbol, interval, fetchFn) {
    var fetchImpl = fetchFn || (typeof fetch === 'function' ? fetch : null);
    if (!fetchImpl) return Promise.resolve([]);
    var url, aggregate;
    if (interval === '1d') {
      url = YAHOO_CHART + encodeURIComponent(symbol) + '?interval=1d&range=1y';
      aggregate = null;
    } else if (interval === '1w') {
      url = YAHOO_CHART + encodeURIComponent(symbol) + '?interval=1wk&range=5y';
      aggregate = null;
    } else {
      url = YAHOO_CHART + encodeURIComponent(symbol) + '?interval=1m&range=5d';
      aggregate = INTERVAL_MINUTES[interval] || 1;
    }
    return fetchImpl(url).then(function (r) {
      if (!r.ok) return [];
      return r.json();
    }).then(function (json) {
      var result = json && json.chart && json.chart.result && json.chart.result[0];
      if (!result) return [];
      var quote = result.indicators && result.indicators.quote && result.indicators.quote[0];
      if (!quote) return [];
      var stamps = result.timestamp || [];
      var bars = [];
      stamps.forEach(function (ts, i) {
        var o = quote.open && quote.open[i], h = quote.high && quote.high[i],
          l = quote.low && quote.low[i], c = quote.close && quote.close[i];
        if (![o, h, l, c].every(isFiniteNumber)) return;
        bars.push({ open: o, high: h, low: l, close: c,
          volume: quote.volume ? quote.volume[i] : null,
          time: new Date(ts * 1000).toISOString() });
      });
      return aggregate ? aggregateBars(bars, aggregate) : bars;
    }).catch(function () { return []; });
  }

  // Load candles for (symbol, interval) into cache (key "SYM|interval").
  // While loading the entry is the string 'loading'; on completion it is
  // the bars array (possibly empty = unavailable). Returns a Promise.
  function loadCandles(symbol, interval, fetchFn, cache, archive) {
    if (!symbol || INTERVALS.indexOf(interval) === -1) return Promise.resolve(null);
    var key = symbol + '|' + interval;
    if (cache[key] && cache[key] !== 'loading') return Promise.resolve(cache[key]);
    if (cache[key] === 'loading') return Promise.resolve(null);
    cache[key] = 'loading';
    var archived = archive ? archive(symbol, interval).catch(function () { return []; }) : Promise.resolve([]);
    return archived.then(function (rows) {
      return rows.length ? rows : yahooBars(symbol, interval, fetchFn);
    }).then(function (bars) {
      cache[key] = bars;
      return bars;
    });
  }

  function renderNews(stock) {
    var news = Array.isArray(stock.news) ? stock.news : [];
    if (!news.length) return '<p class="nq-empty">No headlines in this snapshot for ' + escapeHtml(stock.symbol) + '.</p>';
    return '<ul class="nq-news-list">' + news.map(function (item) {
      var href = safeUrl(item.url);
      var title = escapeHtml(item.title || '(untitled)');
      var link = href ? '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + title + '</a>'
        : '<span>' + title + '</span> <span class="nq-dim">(link rejected)</span>';
      return '<li>' + link + '<span class="nq-news-meta">' + escapeHtml(item.source || 'unknown source') +
        ' · ' + escapeHtml(fmtTime(item.published_at)) + '</span></li>';
    }).join('') + '</ul>';
  }

  function renderDetail(stock, interval, candles, total) {
    var buttons = INTERVALS.map(function (iv) {
      return '<button type="button" class="nq-iv-btn' + (iv === interval ? ' active' : '') +
        '" data-interval="' + iv + '"' + (iv === interval ? ' aria-pressed="true"' : '') + '>' + iv + '</button>';
    }).join('');
    return '<div class="nq-detail-head"><h3>' + escapeHtml(stock.symbol) + ' — ' + escapeHtml(stock.name || '') + '</h3>' +
      '<span class="nq-detail-price">' + fmtPrice(stock.price) + (stock.minuteLabel ? ' · ' + escapeHtml(stock.minuteLabel) : ' · hourly') + '</span>' +
      '<span class="nq-detail-change ' + pctClass(stock.percent_change) + '">' + fmtPct(stock.percent_change) + '</span>' +
      '<span class="nq-dim">Stock %: ' + escapeHtml(STOCK_PCT_LABEL) + '</span>' +
      '<button type="button" class="nq-close" data-close="1">Close ✕</button></div>' +
      '<div class="nq-detail-stats"><span>Day range: ' + fmtPrice(stock.day_low) + ' – ' + fmtPrice(stock.day_high) + '</span>' +
      '<span>Volume: ' + fmtInt(stock.volume) + '</span>' +
      '<span>Market cap: ' + fmtMarketCap(stock.market_cap) + '</span>' +
      '<span>Rank: ' + (isFiniteNumber(stock.rank) ? stock.rank : '—') +
      (isFiniteNumber(total) ? ' of ' + total : '') + '</span></div>' +
      '<div class="nq-intervals" role="group" aria-label="Candle interval">' + buttons + '</div>' +
      '<a href="/one-clock.html?dataset=stock-candles&amp;symbol=' + encodeURIComponent(stock.symbol) +
      '&amp;interval=' + encodeURIComponent(interval) + '&amp;session=' + encodeURIComponent(
        new Intl.DateTimeFormat('en-CA', {timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(stock.as_of || Date.now()))) +
      '">Replay archived candles in Event Builder</a>' + renderCandles(stock.symbol, interval, candles) +
      '<h4>Daily headlines</h4>' + renderNews(stock);
  }

  // Renders the panel from a validated snapshot. state: {query, sortKey,
  // sortDir, view, selected, interval}. `candles` is the on-demand candle cache
  // {SYMBOL: {interval: bars | 'loading'}}; data.candles (if a publisher
  // ever adds it) is used as a fallback. Returns the visible row count.
  // The heatmap is the default view; both views render from the same
  // filtered snapshot rows, so a search filters both and a refresh
  // updates both. The toggle only switches which one is shown.
  function renderSnapshot(doc, data, state, nowMs, candles) {
    candles = candles || data.candles || {};
    var stale = isStale(data.as_of, nowMs);
    var statusEl = doc.getElementById('nasdaq-status');
    var heroEl = doc.getElementById('nasdaq-hero');
    var moversEl = doc.getElementById('nasdaq-movers');
    var tbody = doc.getElementById('nasdaq-tbody');
    var detailEl = doc.getElementById('nasdaq-detail');
    var countEl = doc.getElementById('nasdaq-count');
    var heatmapEl = doc.getElementById('nasdaq-heatmap');
    var changesEl = doc.getElementById('nasdaq-changes');
    var tableWrap = doc.getElementById('nasdaq-table-wrap');
    var viewHeatmapBtn = doc.getElementById('nasdaq-view-heatmap');
    var viewTableBtn = doc.getElementById('nasdaq-view-table');
    var view = state.view === 'table' ? 'table' : 'heatmap';
    var filtered = sortStocks(searchStocks(data.stocks, state.query), state.sortKey, state.sortDir);
    statusEl.textContent = stale
      ? 'STALE — snapshot as of ' + fmtTime(data.as_of) + ' is older than ' + Math.round(STALE_AFTER_MS / 60000) +
        ' min. Showing last known data; do not treat it as current.'
      : 'Snapshot as of ' + fmtTime(data.as_of) + '. ' + SOURCE_LINE + '.';
    statusEl.setAttribute('data-state', stale ? 'stale' : 'live');
    heroEl.innerHTML = renderHero(data, stale);
    moversEl.innerHTML = renderMovers(data.stocks);
    if (changesEl) changesEl.innerHTML = renderIndexChanges(data);
    if (heatmapEl) {
      heatmapEl.innerHTML = renderHeatmap(filtered);
      heatmapEl.hidden = view !== 'heatmap';
    }
    if (tableWrap) tableWrap.hidden = view !== 'table';
    if (viewHeatmapBtn) viewHeatmapBtn.setAttribute('aria-pressed', view === 'heatmap' ? 'true' : 'false');
    if (viewTableBtn) viewTableBtn.setAttribute('aria-pressed', view === 'table' ? 'true' : 'false');
    tbody.innerHTML = renderRows(filtered);
    if (countEl) countEl.textContent = filtered.length + ' of ' + data.stocks.length + ' stocks';
    var selected = null;
    if (state.selected) {
      selected = (data.stocks || []).filter(function (s) { return s.symbol === state.selected; })[0] || null;
    }
    if (selected) {
      detailEl.innerHTML = renderDetail(selected, state.interval, candles, data.stocks.length);
      detailEl.hidden = false;
    } else {
      detailEl.innerHTML = '';
      detailEl.hidden = true;
    }
    return filtered.length;
  }

  function renderFailure(doc, message, kind) {
    var statusEl = doc.getElementById('nasdaq-status');
    var heroEl = doc.getElementById('nasdaq-hero');
    var moversEl = doc.getElementById('nasdaq-movers');
    var tbody = doc.getElementById('nasdaq-tbody');
    var detailEl = doc.getElementById('nasdaq-detail');
    var countEl = doc.getElementById('nasdaq-count');
    var heatmapEl = doc.getElementById('nasdaq-heatmap');
    var changesEl = doc.getElementById('nasdaq-changes');
    var tableWrap = doc.getElementById('nasdaq-table-wrap');
    statusEl.textContent = message;
    statusEl.setAttribute('data-state', kind || 'failed');
    heroEl.innerHTML = '';
    moversEl.innerHTML = '';
    if (heatmapEl) { heatmapEl.innerHTML = ''; heatmapEl.hidden = true; }
    if (changesEl) changesEl.innerHTML = '';
    if (tableWrap) tableWrap.hidden = false;
    tbody.innerHTML = '<tr><td colspan="6" class="nq-empty">' + escapeHtml(message) + '</td></tr>';
    if (countEl) countEl.textContent = '';
    detailEl.innerHTML = '';
    detailEl.hidden = true;
  }

  // fetchJson(url) -> Promise<object>; base is the public-data origin
  // (BROWSER_DATA in the page). opts.nowMs injects the clock for tests.
  function mount(doc, container, fetchJson, opts) {
    opts = opts || {};
    var nowMs = opts.nowMs || Date.now();
    var state = { query: '', sortKey: 'percent_change', sortDir: -1, view: 'heatmap', selected: null, interval: '1d' };
    var data = null;
    var candleCache = {};
    var fetchCandles = opts.fetchCandles || null;
    var statusEl = doc.getElementById('nasdaq-status');
    statusEl.textContent = 'Loading NASDAQ-100 snapshot…';
    statusEl.setAttribute('data-state', 'loading');
    container.setAttribute('data-state', 'loading');

    function rerender() {
      if (!data) return;
      renderSnapshot(doc, data, state, opts.nowMs || Date.now(), candleCache);
    }

    // Fetch candles for the selected stock + interval (on demand, from
    // Yahoo Finance), then re-render the detail view. A failed fetch
    // leaves an empty array in the cache: an explicit "unavailable" state.
    function ensureCandles() {
      if (!state.selected) return;
      loadCandles(state.selected, state.interval, fetchCandles, candleCache, opts.archive).then(function () {
        rerender();
      });
    }

    // One delegated listener set for the whole panel: search, sort, row
    // selection, interval switching and closing the detail view.
    container.addEventListener('input', function (e) {
      if (e.target && e.target.id === 'nasdaq-search') { state.query = e.target.value || ''; rerender(); }
    });
    container.addEventListener('change', function (e) {
      if (!e.target) return;
      if (e.target.id === 'nasdaq-sort') {
        var parts = String(e.target.value || 'percent_change').split(':');
        state.sortKey = parts[0];
        state.sortDir = parts[1] === 'asc' ? 1 : -1;
        rerender();
      }
    });
    container.addEventListener('click', function (e) {
      var t = e.target;
      while (t && t !== container) {
        if (t.getAttribute) {
          if (t.getAttribute('data-close')) { state.selected = null; rerender(); return; }
          var iv = t.getAttribute('data-interval');
          if (iv && INTERVALS.indexOf(iv) !== -1) { state.interval = iv; rerender(); ensureCandles(); return; }
          if (t.getAttribute('data-sort')) {
            var key = t.getAttribute('data-sort');
            if (state.sortKey === key) state.sortDir = -state.sortDir;
            else { state.sortKey = key; state.sortDir = key === 'name' ? 1 : -1; }
            rerender(); return;
          }
          var view = t.getAttribute('data-view');
          if (view === 'heatmap' || view === 'table') { state.view = view; rerender(); return; }
          var sym = t.getAttribute('data-symbol');
          if (sym) { state.selected = sym; rerender(); ensureCandles(); return; }
        }
        t = t.parentNode;
      }
    });

    return fetchJson(ARTIFACT_PATH).then(function (snapshot) {
      var problems = validateSnapshot(snapshot);
      if (problems.length) {
        renderFailure(doc, 'Could not render the NASDAQ-100 snapshot: ' + problems.join('; '), 'failed');
        container.setAttribute('data-state', 'failed');
        return null;
      }
      data = snapshot;
      if (opts.watchMinute) {
        let hourly = snapshot;
        let nextHourlyCheck = Date.now() + 3600000;
        opts.watchMinute(async overlay => {
          if (Date.now() >= nextHourlyCheck || Date.parse(overlay?.baseline_at) > Date.parse(hourly.as_of)) {
            try {
              const newer = await fetchJson(ARTIFACT_PATH);
              if (!validateSnapshot(newer).length && Date.parse(newer.as_of) >= Date.parse(hourly.as_of)) hourly = newer;
            } catch (_) { /* keep the last verified hourly snapshot */ }
            nextHourlyCheck = Date.now() + 3600000;
          }
          data = {...hourly, stocks: PanelMinute.rows('nasdaq', hourly, overlay, Date.now())};
          rerender();
        });
      }
      renderSnapshot(doc, data, state, nowMs);
      container.setAttribute('data-state', isStale(data.as_of, nowMs) ? 'stale' : 'ready');
      return data;
    }).catch(function (e) {
      var absent = e && (e.status === 404 || /404/.test(String(e.message || '')));
      renderFailure(doc, absent
        ? 'NASDAQ-100 snapshot not published yet — nasdaq/latest.json is not available at the data origin.'
        : 'NASDAQ-100 snapshot unavailable: ' + (e && e.message ? e.message : e), absent ? 'absent' : 'failed');
      container.setAttribute('data-state', absent ? 'absent' : 'failed');
      return null;
    });
  }

  var api = { INTERVALS: INTERVALS, ARTIFACT_PATH: ARTIFACT_PATH, STALE_AFTER_MS: STALE_AFTER_MS,
    HEATMAP_FULL_PCT: HEATMAP_FULL_PCT, HEATMAP_MIN_SHARE: HEATMAP_MIN_SHARE,
    HEATMAP_W: HEATMAP_W, HEATMAP_H: HEATMAP_H,
    INDEX_PCT_LABEL: INDEX_PCT_LABEL, STOCK_PCT_LABEL: STOCK_PCT_LABEL, SOURCE_LINE: SOURCE_LINE,
    escapeHtml: escapeHtml, safeUrl: safeUrl, fmtPrice: fmtPrice, fmtPct: fmtPct,
    fmtMarketCap: fmtMarketCap, fmtInt: fmtInt, fmtTime: fmtTime, isStale: isStale,
    validateSnapshot: validateSnapshot, sortStocks: sortStocks, searchStocks: searchStocks,
    topMovers: topMovers, breadth: breadth, renderHero: renderHero, renderMovers: renderMovers,
    renderRows: renderRows, renderCandles: renderCandles, renderNews: renderNews,
    heatmapColor: heatmapColor, applyMinShare: applyMinShare, layoutTreemap: layoutTreemap,
    renderHeatmap: renderHeatmap, renderIndexChanges: renderIndexChanges,
    marketStatusText: marketStatusText, aggregateBars: aggregateBars, yahooBars: yahooBars,
    loadCandles: loadCandles,
    renderDetail: renderDetail, renderSnapshot: renderSnapshot, renderFailure: renderFailure, mount: mount };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NasdaqPanel = api;
})(typeof window !== 'undefined' ? window : this);

// Site wiring: the Stocks tab calls this once, on first open (tab-switching.js).
// BROWSER_DATA is the page's public-data origin (index.html): the bounded
// /browser-data proxy wherever the page uses it, otherwise the public R2
// origin. A missing origin requests nothing and fails visibly.
function loadNasdaqPanel() {
  var container = document.getElementById('nasdaq-panel');
  var base = typeof BROWSER_DATA === 'undefined' ? null : BROWSER_DATA;
  if (base === null || base === undefined || String(base) === '') {
    NasdaqPanel.renderFailure(document,
      'NASDAQ-100 snapshot unavailable: no public data origin is configured for this site, so nothing was requested.',
      'failed');
    container.setAttribute('data-state', 'failed');
    return Promise.resolve(null);
  }
  var root = String(base).replace(/\/+$/, '');
  function fetchJson(path) {
    var url = root + '/' + path + '?_=' + Date.now();
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (r.status === 404) { var e = new Error('HTTP 404'); e.status = 404; throw e; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }
  return NasdaqPanel.mount(document, container, fetchJson, {
    archive: function (symbol, interval) {
      return import('./stock-candle-replay.js').then(function (module) { return module.latestStockCandles(symbol, interval); });
    },
    watchMinute: typeof PanelMinute === 'undefined' ? null : cb => PanelMinute.watch('nasdaq', root, cb)
  });
}
