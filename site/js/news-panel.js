// News panel (MediaFlow M4b; G6 contract): renders the sanitized news/v2/views/
// pages published from market_data/news/views.py builds. Classic script, loaded by index.html between
// squeeze-panel.js and tab-switching.js; the News tab mounts it once, on first
// open (tab-switching.js calls loadNewsPanel). This is the only copy: the
// tests-only harness (tests/news_panel/harness.html) loads this file.
// Text goes through textContent only; links are re-checked with new URL() at
// render time. The panel never infers a quiet interval: "Observed, no items"
// appears only where the builder marked a coverage segment quiet (whole-span
// coverage, no new observations and no row events in the span). Only the
// latest view or page request may render.
// Every request goes through makeFetcher, which applies the exact view-key
// check (isNewsViewKey, the same rule as backend/browser-data.js) before
// fetching, on both the /browser-data proxy and the direct public-R2 path, so
// a key taken from a page (first_page, the picker, prev/next) that fails is
// shown as a failure and never requested. "No news views published yet" is
// shown for a 404 on index.json and for an "unpublished" tombstone; a
// "withdrawn" tombstone shows "News views are not currently published". Neither
// requests a page. G3/G6: pages live in immutable incarnations of a generation
// (news/v2/views/p/<gen>.<n>/...); a reader follows only links of the
// incarnation its index names, for at most LEASE_MS from the index request; a
// page 404, a request past its deadline, an incarnation mismatch or an ended
// lease shows "refresh required" (reload the fixed index), never another
// incarnation's pages. Every other problem is a failure.
(function (root) {
  'use strict';

  var VIEW_SCHEMA = 'news-view/v1';
  var INDEX_SCHEMA = 'news-view-index/v2';
  var INDEX_KEY = 'news/v2/views/index.json';
  // Mirrors views.py: MODES, STREAM_KEYS and Limits.pages_per_view (the index records all three).
  var MODES = ['collector', 'archive', 'publication'];
  var STREAMS = ['all', 'ai_buildout', 'oil'];
  var MAX_PAGES = 20;
  // G6: pages are immutable and qualified by an incarnation of a generation
  // (news/v2/views/p/<gen>.<n>/...); index.json is the fixed commit point.
  // <gen> = YYYYMMDDTHHMMSSZ-<16 hex>, <n> = 1..9999 with no leading zero. The index's "gen" and every
  // page's "gen" carry the incarnation id <gen>.<n>. Same rule as publish.py, backend/browser-data.js
  // and frontend/preview-policy.mjs. Legacy news/v1/views keys are refused.
  var GEN = /^([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z-[0-9a-f]{16}\.[1-9][0-9]{0,3}$/;
  var VIEW_KEY = /^news\/v2\/views\/p\/([0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.[1-9][0-9]{0,3})\/(collector|archive|publication)\/(all|ai_buildout|oil)\/([0-9]{4})-([0-9]{2})-([0-9]{2})\/page-([0-9]{4})\.json$/;
  var RESEARCH_KEY = /^news\/v2\/views\/p\/([0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.[1-9][0-9]{0,3})\/research\/view\.json$/;
  var NONCE = /^[0-9a-f]{32}$/;
  // Owner-approved defaults (config.GEN_*): a reader follows one index for at most LEASE_MS from the
  // moment its request started; every request has a DEADLINE_MS deadline.
  var LEASE_MS = 30 * 60 * 1000;
  var DEADLINE_MS = 20 * 1000;
  var CLOCK_AGREE_MS = 2 * 60 * 1000;
  var URL_MAX = 2048;
  // Whitespace, C0/C1 controls and invisible format characters (bidi, zero-width).
  var BAD_URL_CHARS = /[\s\u0000-\u001f\u007f-\u009f­​-‏‪-‮⁠-⁤﻿]/;
  var STATE_TEXT = {
    covered: 'covered',
    partial: 'partial: some sources unknown',
    unknown: 'unknown: no relevant source fetched',
    pending_availability: 'pending availability: not yet archived'
  };

  function safeUrl(value) {
    if (typeof value !== 'string' || value === '' || value.length > URL_MAX) return null;
    if (BAD_URL_CHARS.test(value)) return null;
    // The raw text must already have an authority: URL() would repair "http:///host" or
    // "http:\\host" into a link that the builder (urllib) never accepted.
    if (value.indexOf('\\') !== -1 || !/^https?:\/\/[^/?#]/i.test(value)) return null;
    var u;
    try { u = new URL(value); } catch (e) { return null; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    if (!u.hostname || u.username || u.password) return null;
    // A userinfo separator anywhere before the path is refused even if URL() dropped it.
    var authority = value.replace(/^[a-z]+:\/\//i, '').split(/[/?#]/)[0];
    if (authority.indexOf('@') !== -1) return null;
    return u.href;
  }

  function realDate(y, mo, d) {
    var date = y + '-' + mo + '-' + d;
    var t = new Date(date + 'T00:00:00Z');
    return isFinite(t.getTime()) && t.toISOString().slice(0, 10) === date;
  }

  // An incarnation id (<gen>.<n>) whose generation has a real UTC instant.
  function isGen(value) {
    if (typeof value !== 'string') return false;
    var m = GEN.exec(value);
    if (!m || !realDate(m[1], m[2], m[3])) return false;
    return Number(m[4]) < 24 && Number(m[5]) < 60 && Number(m[6]) < 60;
  }

  // The incarnation a published page key belongs to, or null.
  function genOf(key) {
    if (typeof key !== 'string') return null;
    var m = VIEW_KEY.exec(key);
    if (!m || !isGen(m[1])) return null;
    var n = Number(m[7]);
    if (n < 1 || n > MAX_PAGES || !realDate(m[4], m[5], m[6])) return null;
    return m[1];
  }

  // MediaFlow R1: the incarnation of a generation's one research page (p/<gen>.<n>/research/view.json),
  // or null. Kept apart from genOf so a research key never stands in for a view page.
  function researchGenOf(key) {
    if (typeof key !== 'string') return null;
    var m = RESEARCH_KEY.exec(key);
    return m && isGen(m[1]) ? m[1] : null;
  }

  // The index, one incarnation / mode / stream, a real calendar date and page 0001-0020, or one
  // incarnation's research page. Nothing else.
  function isNewsViewKey(key) {
    return key === INDEX_KEY || genOf(key) !== null || researchGenOf(key) !== null;
  }

  function newsError(kind, message) {
    var e = new Error(message);
    e.newsKind = kind;  // refused | unconfigured | absent | gone | deadline | http | network | json | invalid | render
    return e;
  }

  function quoteKey(key) {
    var s = typeof key === 'string' ? key : String(key);
    return JSON.stringify(s.length > 120 ? s.slice(0, 120) + '…' : s);
  }

  // base: NEWS_DATA ('/browser-data' wherever the page uses the bounded route, otherwise the dedicated
  // public News origin, or null while none is provisioned). A null or empty base requests nothing: every
  // call fails visibly with kind 'unconfigured' (never a false empty state, never a relative URL).
  // Returns fetchJson(key) -> Promise<object>. The key check runs before any request is made. Every
  // request (headers and body) must finish within deadlineMs (default DEADLINE_MS) or it fails with
  // kind 'deadline' and its late body is discarded; it is aborted when AbortController exists. Requests
  // ask for no cached copy (cache: 'no-store'); the existing ?_= query stays.
  function makeFetcher(base, fetchImpl, now, deadlineMs, timers) {
    if (base === null || base === undefined || String(base) === '') {
      return function fetchJson() {
        return Promise.reject(newsError('unconfigured',
          'no public News origin is configured for this site, so nothing was requested'));
      };
    }
    var root = String(base).replace(/\/+$/, '');
    var limit = deadlineMs || DEADLINE_MS;
    var T = timers || { set: function (f, ms) { return setTimeout(f, ms); }, clear: function (h) { clearTimeout(h); } };
    return function fetchJson(key) {
      if (!isNewsViewKey(key)) {
        return Promise.reject(newsError('refused', 'refused: not a published news view key, so it was not requested'));
      }
      var url = root + '/' + key + '?_=' + Math.floor(now());
      var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
      var timer = null;
      var expired = new Promise(function (_, reject) {
        timer = T.set(function () {
          if (ctrl) { try { ctrl.abort(); } catch (e) { /* already settled */ } }
          reject(newsError('deadline', key + ': no complete response within ' + Math.round(limit / 1000) + ' s'));
        }, limit);
      });
      var work = Promise.resolve().then(function () {
        var init = { credentials: 'omit', cache: 'no-store' };
        if (ctrl) init.signal = ctrl.signal;
        return fetchImpl(url, init);
      }).then(function (r) {
        if (r.status === 404 && key === INDEX_KEY) throw newsError('absent', 'No news views published yet');
        if (r.status === 404) throw newsError('gone', key + ': no longer published (HTTP 404)');
        if (!r.ok) throw newsError('http', key + ': HTTP ' + r.status);
        return r.text().then(function (body) {
          try { return JSON.parse(body); } catch (e) { throw newsError('json', key + ': response is not valid JSON'); }
        });
      }, function (e) {
        throw newsError('network', key + ': request failed (' + (e && e.message ? e.message : e) + ')');
      });
      return Promise.race([work, expired]).then(function (v) { T.clear(timer); return v; },
        function (e) { T.clear(timer); throw e; });
    };
  }

  function isObj(x) { return x !== null && typeof x === 'object' && !Array.isArray(x); }

  // The structure the renderer relies on, checked before a page is committed as ready.
  // A page that fails is a visible failure, never a blank panel.
  function validatePage(page) {
    if (!isObj(page)) return ['not an object'];
    var problems = [];
    var need = function (ok, what) { if (!ok) problems.push(what); };
    if (page.schema_version !== VIEW_SCHEMA) problems.push('schema_version is not ' + VIEW_SCHEMA);
    ['data_label', 'mode', 'as_of', 'window', 'stream', 'date', 'page_number', 'page_count', 'view',
      'complete', 'overflow_reasons', 'omitted', 'coverage', 'rows'].forEach(function (k) {
      if (!(k in page)) problems.push('missing ' + k);
    });
    if (problems.length) return problems;
    ['data_label', 'mode', 'as_of', 'stream', 'date'].forEach(function (k) {
      need(typeof page[k] === 'string', k + ' is not text');
    });
    need(isObj(page.window) && typeof page.window.start === 'string' && typeof page.window.end === 'string',
      'window is not {start, end}');
    need(Number.isInteger(page.page_number) && Number.isInteger(page.page_count), 'page_number/page_count are not integers');
    need(isObj(page.view) && Number.isInteger(page.view.rows_total), 'view is not an object with rows_total');
    need(typeof page.complete === 'boolean', 'complete is not true/false');
    need(Array.isArray(page.overflow_reasons), 'overflow_reasons is not a list');
    need(isObj(page.omitted), 'omitted is not an object');
    var cov = page.coverage;
    need(isObj(cov), 'coverage is not an object');
    if (isObj(cov)) {
      need(Array.isArray(cov.segments) && cov.segments.every(isObj), 'coverage.segments is not a list of objects');
      need(Array.isArray(cov.after_cutoff_hindsight), 'coverage.after_cutoff_hindsight is not a list');
      need(isObj(cov.price), 'coverage.price is not an object');
    }
    need(Array.isArray(page.rows) && page.rows.every(isObj), 'rows is not a list of objects');
    need(!Array.isArray(page.rows) || !page.rows.length || Number.isInteger(page.rows_offset),
      'rows_offset is not an integer');
    return problems;
  }

  // A tombstone: the index key holds no publication. 'withdrawn' (explicit withdrawal) or
  // 'unpublished' (before the first publication), else null. It must be exactly a tombstone: the v2
  // schema, that state, a commit nonce, and no generation or views.
  function tombstoneState(index) {
    if (!isObj(index) || index.schema_version !== INDEX_SCHEMA) return null;
    if (index.state !== 'withdrawn' && index.state !== 'unpublished') return null;
    if (!isObj(index.commit) || typeof index.commit.nonce !== 'string' || !NONCE.test(index.commit.nonce)) return null;
    if ('gen' in index || 'views' in index) return null;
    return index.state;
  }

  function validateIndex(index) {
    var problems = [];
    if (!index || index.schema_version !== INDEX_SCHEMA) problems.push('schema_version is not ' + INDEX_SCHEMA);
    if (!index || index.state !== 'published') problems.push('state is not published');
    if (!index || !isObj(index.commit) || typeof index.commit.nonce !== 'string' || !NONCE.test(index.commit.nonce)) {
      problems.push('commit.nonce is not a 128-bit hex nonce');
    }
    if (!index || !Array.isArray(index.views)) problems.push('views is not a list');
    if (!index || !isGen(index.gen)) problems.push('gen is not a generation incarnation id');
    if (index && isGen(index.gen) && index.logical_gen !== index.gen.slice(0, index.gen.lastIndexOf('.'))) {
      problems.push('logical_gen is not the generation of gen');
    }
    if (!index || typeof index.pages_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(index.pages_sha256)) {
      problems.push('pages_sha256 is not a SHA-256');
    }
    if (index && Array.isArray(index.views) && isGen(index.gen)) {
      index.views.forEach(function (v, i) {
        if (!isObj(v) || genOf(v.first_page) !== index.gen) problems.push('views[' + i + '].first_page is not in generation ' + index.gen);
      });
    }
    return problems;
  }

  // --- tiny DOM helpers (textContent only) ---------------------------------
  function el(doc, tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function add(parent) {
    for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(arguments[i]);
    return parent;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function t(iso) { return iso ? String(iso).replace('T', ' ').replace(/(\.\d+)?Z$/, 'Z') : 'none'; }
  function hms(iso) { return iso ? String(iso).slice(11, 19) : 'start'; }
  function secs(n) {
    if (n === null || n === undefined) return '';
    if (n < 120) return n + ' s';
    if (n < 7200) return Math.round(n / 60) + ' min';
    return (n / 3600).toFixed(1) + ' h';
  }

  function badge(doc, text, kind) { return el(doc, 'span', 'np-badge' + (kind ? ' np-' + kind : ''), text); }

  function header(doc, page) {
    var h = el(doc, 'div', 'np-header');
    add(h, badge(doc, page.data_label, 'synthetic'));
    add(h, badge(doc, page.mode + ' mode', page.hindsight ? 'hindsight' : 'mode'));
    if (page.hindsight) add(h, badge(doc, 'RETROSPECTIVE (hindsight)', 'hindsight'));
    add(h, el(doc, 'span', 'np-dim', 'stream ' + page.stream + ' · ' + page.date + ' UTC'));
    var meta = el(doc, 'div', 'np-meta');
    add(meta, el(doc, 'span', null, 'as of ' + t(page.as_of)));
    add(meta, el(doc, 'span', null, 'window ' + t(page.window.start) + ' → ' + t(page.window.end)));
    if (page.price_series) {
      add(meta, el(doc, 'span', null, 'prices ' + page.price_series.instrument_id + ' ' + page.price_series.series +
        ' (tolerance ' + page.price_series.tolerance_s + ' s' + (page.price_series.synthetic ? ', SYNTHETIC' : '') + ')'));
    }
    add(meta, el(doc, 'span', 'np-dim', page.mode_label));
    return add(el(doc, 'div', 'np-head'), h, meta);
  }

  function incompleteBanner(doc, page) {
    var cov = page.coverage;
    if (page.complete && cov.coverage_complete) return null;
    var b = el(doc, 'div', 'np-incomplete');
    b.setAttribute('role', 'alert');
    add(b, el(doc, 'strong', null, 'INCOMPLETE'));
    var parts = [];
    if (!cov.coverage_complete) parts.push('coverage is incomplete: omitted coverage is not a quiet interval');
    (page.overflow_reasons || []).forEach(function (r) { parts.push(r.replace(/_/g, ' ')); });
    var o = page.omitted || {};
    Object.keys(o).sort().forEach(function (k) { if (o[k]) parts.push(k.replace(/_/g, ' ') + ': ' + o[k]); });
    if (page.view && page.view.pages_omitted) parts.push('pages omitted: ' + page.view.pages_omitted);
    add(b, el(doc, 'span', null, ' ' + parts.join(' · ')));
    return b;
  }

  function nav(doc, page, onNavigate) {
    var n = el(doc, 'div', 'np-nav');
    var prev = el(doc, 'button', 'np-btn', '← newer');
    var next = el(doc, 'button', 'np-btn', 'older →');
    prev.disabled = !page.prev_page;
    next.disabled = !page.next_page;
    prev.setAttribute('data-key', page.prev_page || '');
    next.setAttribute('data-key', page.next_page || '');
    if (onNavigate) {
      prev.addEventListener('click', function () { if (page.prev_page) onNavigate(page.prev_page); });
      next.addEventListener('click', function () { if (page.next_page) onNavigate(page.next_page); });
    }
    var total = page.view.rows_total;
    add(n, prev, el(doc, 'span', 'np-pageno', 'Page ' + page.page_number + ' of ' + page.page_count +
      ' · rows ' + (page.rows.length ? (page.rows_offset + 1) + '–' + (page.rows_offset + page.rows.length) : '0') +
      ' of ' + total + (page.view.rows_omitted ? ' (' + page.view.rows_omitted + ' omitted)' : '')), next);
    return n;
  }

  function segmentText(seg) {
    if (seg.quiet) return 'Observed, no items';
    var s = STATE_TEXT[seg.state] || seg.state;
    if (seg.before_first_run) s += ' · before the first observed run';
    return s;
  }

  // New observations (by observed_at) and row events (by event_at) are separate counts:
  // a delayed archive write or classification can put a row's event after its receipt.
  function spanCounts(seg) {
    return seg.observations_in_span + ' new observed · ' + seg.events_in_span + ' events';
  }

  function coverage(doc, page) {
    var cov = page.coverage;
    var box = el(doc, 'section', 'np-coverage');
    add(box, el(doc, 'h3', null, 'News coverage (' + cov.stream_scope + ')'));
    add(box, el(doc, 'p', 'np-note', cov.note));
    if (cov.quiet_blocked_by && cov.quiet_blocked_by.length) {
      add(box, el(doc, 'p', 'np-note', 'No span is called quiet in this view: ' +
        cov.quiet_blocked_by.map(function (r) { return r.replace(/_/g, ' '); }).join(', ')));
    }
    var wrap = el(doc, 'div', 'np-table-wrap');
    var table = el(doc, 'table', 'np-table');
    var head = el(doc, 'tr');
    ['Span (UTC)', 'State', 'In span', 'Sources'].forEach(function (c) { add(head, el(doc, 'th', null, c)); });
    add(table, head);
    if (!cov.segments.length) {
      var tr0 = el(doc, 'tr');
      var td0 = el(doc, 'td', 'np-unknown', 'No observed run in this window: coverage unknown');
      td0.setAttribute('colspan', '4');
      add(table, add(tr0, td0));
    }
    cov.segments.forEach(function (seg) {
      var tr = el(doc, 'tr', 'np-seg np-' + (seg.quiet ? 'quiet' : seg.state));
      tr.setAttribute('data-state', seg.state);
      tr.setAttribute('data-quiet', seg.quiet ? 'true' : 'false');
      var span = hms(seg.start) + ' → ' + hms(seg.end) + (seg.clipped_to_window_start ? ' (clipped)' : '');
      var st = el(doc, 'td', null, segmentText(seg));
      if (seg.missed_interval) add(st, el(doc, 'span', 'np-badge np-missed', 'missed interval ' + secs(seg.gap_seconds)));
      var src = seg.sources;
      var srcText = src ? (src.ok.length + ' ok of ' + src.attempted.length + ' fetched' +
        (src.failed.length ? ' · failed: ' + src.failed.map(function (f) { return f.source_id + ' (' + f.status + ')'; }).join(', ') : '') +
        (src.not_selected.length ? ' · not selected: ' + src.not_selected.join(', ') : '') +
        (src.failed_entries ? ' · ' + src.failed_entries + ' failed entries' : '') +
        (src.disabled_count ? ' · ' + src.disabled_count + ' disabled' : '') +
        (src.roster_complete ? '' : ' · roster unknown') +
        (src.source_ids_omitted ? ' · ' + src.source_ids_omitted + ' ids omitted' : '')) : 'not reported';
      add(table, add(tr, el(doc, 'td', 'np-mono', span), st, el(doc, 'td', 'np-num', spanCounts(seg)),
        el(doc, 'td', 'np-src', srcText)));
    });
    add(box, add(wrap, table));
    var facts = el(doc, 'ul', 'np-facts');
    if (cov.tail) add(facts, el(doc, 'li', 'np-unknown', 'Since the last observed run: ' + hms(cov.tail.start) + ' → ' +
      hms(cov.tail.end) + ' (' + secs(cov.tail.seconds) + ') not yet observed'));
    if (cov.after_cutoff_hindsight.length) add(facts, el(doc, 'li', 'np-dim', 'Runs after the cutoff (hindsight only): ' +
      cov.after_cutoff_hindsight.map(function (h) { return hms(h.end) + ' ' + h.state; }).join(', ')));
    if (cov.pending_availability_runs) add(facts, el(doc, 'li', 'np-unknown', cov.pending_availability_runs + ' run(s) pending availability'));
    if (cov.corrupt_runs) add(facts, el(doc, 'li', 'np-bad', cov.corrupt_runs + ' corrupt run(s)'));
    if (cov.uncommitted_runs) add(facts, el(doc, 'li', 'np-bad', cov.uncommitted_runs + ' uncommitted run(s)'));
    ['missing', 'stale'].forEach(function (k) {
      (cov.price[k] || []).forEach(function (i) {
        add(facts, el(doc, 'li', 'np-price-' + k, 'Price ' + k + ' ' + hms(i.start) + ' → ' + hms(i.end) + ' (' + secs(i.seconds) + ')' +
          (i.last_price_time ? ', last price ' + hms(i.last_price_time) : '')));
      });
    });
    if (facts.firstChild) add(box, facts);
    return box;
  }

  function priceText(p) {
    if (p.status === 'missing' || !p.record) return 'price missing: none available yet';
    var r = p.record;
    return 'price ' + p.status + ': ' + r.price + ' (' + r.record_id + ', age ' + secs(p.age_seconds) + ')' +
      (r.synthetic ? ' SYNTHETIC' : '');
  }

  function row(doc, r) {
    var card = el(doc, 'article', 'np-row');
    card.setAttribute('data-revision', r.revision_id);
    var title = el(doc, 'div', 'np-title');
    var href = safeUrl(r.canonical_url);
    if (href) {
      var a = el(doc, 'a', null, r.title);
      a.setAttribute('href', href);
      a.setAttribute('rel', 'noopener noreferrer');
      a.setAttribute('target', '_blank');
      add(title, a);
    } else {
      add(title, el(doc, 'span', null, r.title));
      add(title, badge(doc, 'link rejected' + (r.url_rejected ? ': ' + r.url_rejected.replace(/_/g, ' ') : ''), 'bad'));
    }
    if (r.title_truncated) add(title, badge(doc, 'title truncated', 'dim'));
    add(card, title);
    var line = el(doc, 'div', 'np-line');
    add(line, el(doc, 'span', null, (r.outlet ? r.outlet + ' · ' : '') + r.source_id));
    add(line, el(doc, 'span', 'np-mono', 'event ' + t(r.event_at) + ' (' + r.event_basis + ')'));
    add(card, line);
    var times = el(doc, 'div', 'np-line np-dim');
    var tq = r.time_quality || {};
    add(times, el(doc, 'span', null, 'published ' + (r.published_at ? t(r.published_at) : 'unknown') +
      ' [' + (tq.published || 'unknown') + ']'));
    add(times, el(doc, 'span', null, 'observed ' + t(r.observed_at)));
    add(times, el(doc, 'span', null, 'archived ' + (r.archived_at ? t(r.archived_at) : 'pending')));
    add(card, times);
    var ctx = el(doc, 'div', 'np-line');
    add(ctx, el(doc, 'span', null, 'revision ' + r.revision_number +
      (r.revisions_known_at_event !== null ? ' (' + r.revisions_known_at_event + ' known at event)' : '')));
    var cls = r.classification_at_event;
    var retro = cls && cls.timing !== 'at_event';
    add(ctx, el(doc, 'span', null, !cls ? 'unclassified at event' : retro ? 'streams at event: not known' :
      'streams at event: ' + (cls.stream_memberships.join(', ') || 'none')));
    add(ctx, badge(doc, 'instrument: ' + (r.instruments.mapping === 'unknown' ? 'unmapped' : r.instruments.mapping), 'dim'));
    add(card, ctx);
    var lc = r.later_context;
    var later = [];
    if (retro) {
      later.push('classified ' + hms(cls.classified_at) + ' (retrospective): ' + (cls.stream_memberships.join(', ') || 'none'));
    }
    (lc.revisions_after_event || []).forEach(function (x) { later.push('revised ' + hms(x.known_at) + ': ' + x.title); });
    if (lc.revisions_after_event_omitted) later.push(lc.revisions_after_event_omitted + ' more revisions');
    (lc.classifications_after_event || []).forEach(function (x) {
      later.push('reclassified ' + hms(x.known_at) + ': ' + (x.stream_memberships.join(', ') || 'none'));
    });
    if (lc.classifications_after_event_omitted) later.push(lc.classifications_after_event_omitted + ' more classifications');
    if (lc.stream_membership_removed_at) later.push('left the stream ' + hms(lc.stream_membership_removed_at));
    if (later.length) add(card, el(doc, 'div', 'np-later', 'Later context (not known at event): ' + later.join(' · ')));
    add(card, el(doc, 'div', 'np-price np-price-' + r.price.status, priceText(r.price)));
    return card;
  }

  function rowsSection(doc, page, sourceFilter, onFilter) {
    var box = el(doc, 'section', 'np-rows');
    var bar = el(doc, 'div', 'np-filter');
    var sources = [];
    page.rows.forEach(function (r) { if (sources.indexOf(r.source_id) === -1) sources.push(r.source_id); });
    sources.sort();
    var select = el(doc, 'select', 'np-select');
    select.setAttribute('aria-label', 'Source filter (this page)');
    var all = el(doc, 'option', null, 'all sources');
    all.value = '';
    add(select, all);
    sources.forEach(function (s) { var o = el(doc, 'option', null, s); o.value = s; if (s === sourceFilter) o.selected = true; add(select, o); });
    select.value = sourceFilter || '';
    if (onFilter) select.addEventListener('change', function () { onFilter(select.value || null); });
    var shown = page.rows.filter(function (r) { return !sourceFilter || r.source_id === sourceFilter; });
    add(bar, select, el(doc, 'span', 'np-count', shown.length + ' of ' + page.rows.length + ' rows on this page'));
    if (sourceFilter) add(bar, el(doc, 'span', 'np-note', 'Coverage above stays for the whole stream, not this source.'));
    add(box, bar);
    if (!shown.length) {
      add(box, el(doc, 'p', 'np-empty', sourceFilter ? 'No rows from ' + sourceFilter + ' on this page' : 'No items on this page'));
    }
    shown.forEach(function (r) { add(box, row(doc, r)); });
    return box;
  }

  function renderPage(doc, container, page, opts) {
    opts = opts || {};
    clear(container);
    var problems = validatePage(page);
    if (problems.length) {
      add(container, el(doc, 'div', 'np-incomplete', 'Cannot render this page: ' + problems.join('; ')));
      return;
    }
    var rerender = function (filter) { renderPage(doc, container, page, Object.assign({}, opts, { sourceFilter: filter })); };
    add(container, header(doc, page), incompleteBanner(doc, page), nav(doc, page, opts.onNavigate),
      coverage(doc, page), rowsSection(doc, page, opts.sourceFilter || null, rerender));
  }

  function indexBanner(doc, index) {
    if (index.complete) return null;
    var b = el(doc, 'div', 'np-incomplete');
    b.setAttribute('role', 'alert');
    add(b, el(doc, 'strong', null, 'INCOMPLETE index'));
    add(b, el(doc, 'span', null, ' ' + index.views_omitted + ' of ' + index.views_total + ' views omitted (' +
      (index.overflow_reasons || []).map(function (r) { return r.replace(/_/g, ' '); }).join(', ') + ')'));
    return b;
  }

  function viewPicker(doc, index, current, onPick) {
    var box = el(doc, 'div', 'np-picker');
    var label = el(doc, 'span', 'np-dim', index.data_label + ' · build as of ' + t(index.build_as_of));
    var select = el(doc, 'select', 'np-select');
    select.setAttribute('aria-label', 'View (mode / stream / date)');
    index.views.forEach(function (v) {
      var o = el(doc, 'option', null, v.mode + ' / ' + v.stream + ' / ' + v.date + ' · ' + v.rows_total + ' rows' +
        (v.complete ? '' : ' · INCOMPLETE'));
      o.value = v.first_page;
      if (v.first_page === current) o.selected = true;
      add(select, o);
    });
    select.value = current;
    select.addEventListener('change', function () { onPick(select.value); });
    add(box, select, label, indexBanner(doc, index));
    return box;
  }

  // The reader's lease on one index (G3). Both clocks start when the index request starts; the lease
  // holds only while the monotonic and wall clocks both show less than leaseMs, the wall clock has not
  // gone backwards, and the two agree within CLOCK_AGREE_MS. Sleep (monotonic paused, wall advanced) and
  // clock changes therefore end it. clocks: { mono: () => ms, wall: () => ms }.
  function startLease(clocks, leaseMs) {
    var m0 = clocks.mono(), w0 = clocks.wall();
    return function valid() {
      var m = clocks.mono() - m0, w = clocks.wall() - w0;
      return m >= 0 && m < leaseMs && w >= 0 && w < leaseMs && Math.abs(m - w) < CLOCK_AGREE_MS;
    };
  }

  function defaultClocks() {
    var perf = typeof performance !== 'undefined' && performance && typeof performance.now === 'function' ? performance : null;
    return { mono: perf ? function () { return perf.now(); } : function () { return Date.now(); },
      wall: function () { return Date.now(); } };
  }

  // fetchJson(key) -> Promise<object>; keys are paths under the views root.
  // opts (all optional): clocks {mono, wall}, leaseMs, events (an object with addEventListener, e.g.
  // window: the lease is rechecked on visibilitychange, pageshow and online).
  function mount(doc, container, fetchJson, indexKey, opts) {
    opts = opts || {};
    var clocks = opts.clocks || defaultClocks();
    var leaseMs = opts.leaseMs || LEASE_MS;
    var pickerSlot = el(doc, 'div');
    var pageSlot = el(doc, 'div');
    add(container, pickerSlot, pageSlot);
    container.setAttribute('data-state', 'loading');
    var leaseValid = startLease(clocks, leaseMs);   // from the moment the index request starts
    var dead = false;                               // set once this mount has asked for a refresh
    // MediaFlow R1: opts.onIndex(event) lets the research view follow this mount's one index read
    // (same generation, same lease) instead of fetching anything of its own. Its failures never reach
    // the news views. event.state: published | withdrawn | unpublished | absent | failed | refresh.
    function tell(event) {
      if (typeof opts.onIndex !== 'function') return;
      try { opts.onIndex(event); } catch (e) { /* the research view reports its own failure */ }
    }

    // One-shot per mount: the first call ends this mount and starts exactly one new one (one index read);
    // every later call on this mount, from any control, does nothing.
    var reloaded = false;
    function reloadAll() {
      if (reloaded) return false;
      reloaded = true;
      dead = true;
      clear(container);
      mount(doc, container, fetchJson, indexKey, opts);
      return true;
    }

    // Never mixes generations and never falls back: the reader reloads the fixed index instead.
    function refresh(reason) {
      if (dead) return;
      dead = true;
      tell({ state: 'refresh', reason: reason });
      clear(pickerSlot);
      clear(pageSlot);
      container.removeAttribute('data-pending');
      container.removeAttribute('data-page');
      var box = el(doc, 'div', 'np-incomplete np-refresh');
      box.setAttribute('role', 'alert');
      add(box, el(doc, 'strong', null, 'This view was replaced or has expired. '),
        el(doc, 'span', null, 'Reload to see the current publication (' + reason + ').'));
      var button = el(doc, 'button', 'np-btn', 'Reload');
      button.addEventListener('click', function () { reloadAll(); });
      add(box, button);
      add(pageSlot, box);
      container.setAttribute('data-state', 'refresh-required');
    }

    return fetchJson(indexKey).then(function (index) {
      var tomb = tombstoneState(index);
      if (tomb) {
        // Nothing is published: no page is requested and nothing from an earlier mount is kept.
        clear(pickerSlot);
        clear(pageSlot);
        add(pageSlot, el(doc, 'p', 'np-empty np-absent', tomb === 'withdrawn' ?
          'News views are not currently published' : 'No news views published yet'));
        container.removeAttribute('data-pending');
        container.setAttribute('data-page', '');
        container.setAttribute('data-state', tomb === 'withdrawn' ? 'withdrawn' : 'absent');
        tell({ state: tomb });
        return null;
      }
      var problems = validateIndex(index);
      if (problems.length) throw new Error(problems.join('; '));
      // reload: the shared-index reload for the research view's own recovery. It ends this mount and
      // mounts again, so news views and research both follow the one fresh index read (never two).
      tell({ state: 'published', index: index, fetchJson: fetchJson, leaseValid: leaseValid, reload: reloadAll });
      var latest = 0;
      if (opts.events && typeof opts.events.addEventListener === 'function') {
        var recheck = function () { if (!dead && !leaseValid()) refresh('the index lease ended'); };
        ['visibilitychange', 'pageshow', 'online'].forEach(function (type) { opts.events.addEventListener(type, recheck); });
      }
      function show(key) {
        if (dead) return Promise.resolve();
        if (!leaseValid()) { refresh('the index lease ended'); return Promise.resolve(); }
        if (genOf(key) !== index.gen) { refresh('a link left generation ' + index.gen); return Promise.resolve(); }
        var ticket = ++latest;
        container.setAttribute('data-pending', key);
        // One failure path for fetch errors, invalid pages and render errors: the panel shows the
        // failure and drops the ready state and page name, so nothing claims the failed load succeeded.
        function fail(e) {
          container.removeAttribute('data-pending');
          container.removeAttribute('data-page');
          clear(pageSlot);
          add(pageSlot, el(doc, 'div', 'np-incomplete', 'Could not load ' + quoteKey(key) + ': ' +
            (e && e.message ? e.message : e)));
          container.setAttribute('data-state', 'failed');
        }
        return fetchJson(key).then(function (page) {
          if (dead || ticket !== latest) return;  // refreshed, or a newer choice was made while this one loaded
          if (!leaseValid()) { refresh('the index lease ended while the page loaded'); return; }
          if (!isObj(page) || page.gen !== index.gen) { refresh('the page is not from generation ' + index.gen); return; }
          try {
            var problems = validatePage(page);
            if (problems.length) throw newsError('invalid', 'not a valid news view page (' + problems.join('; ') + ')');
            renderPage(doc, pageSlot, page, { onNavigate: show });
            clear(pickerSlot);
            add(pickerSlot, viewPicker(doc, index, key.replace(/page-\d{4}\.json$/, 'page-0001.json'), show));
          } catch (e) {
            fail(e.newsKind ? e : newsError('render', 'the page could not be rendered (' + (e && e.message ? e.message : e) + ')'));
            return;
          }
          container.removeAttribute('data-pending');
          container.setAttribute('data-page', key);
          container.setAttribute('data-state', 'ready');
        }, function (e) {
          if (dead || ticket !== latest) return;  // a stale failure must not replace the newer choice either
          if (e && (e.newsKind === 'gone' || e.newsKind === 'deadline')) { refresh(e.message); return; }
          fail(e);
        });
      }
      if (!index.views.length) {
        add(pickerSlot, add(el(doc, 'div', 'np-picker'),
          el(doc, 'span', 'np-dim', index.data_label + ' · build as of ' + t(index.build_as_of)), indexBanner(doc, index)));
        add(pageSlot, el(doc, 'p', 'np-empty', index.complete ? 'No views in this index' :
          'No view is listed: the index was cut before its first entry'));
        container.setAttribute('data-page', '');
        container.setAttribute('data-state', index.complete ? 'empty' : 'failed');
        return null;
      }
      return show(index.views[0].first_page);
    }).catch(function (e) {
      clear(pageSlot);
      tell({ state: e && e.newsKind === 'absent' ? 'absent' : 'failed' });
      if (e && e.newsKind === 'absent') {
        add(pageSlot, el(doc, 'p', 'np-empty np-absent', 'No news views published yet'));
        container.setAttribute('data-state', 'absent');
        return;
      }
      add(pageSlot, el(doc, 'div', 'np-incomplete', 'Could not load news views: ' + (e && e.message ? e.message : e)));
      container.setAttribute('data-state', 'failed');
    });
  }

  var api = { safeUrl: safeUrl, validatePage: validatePage, validateIndex: validateIndex, renderPage: renderPage,
    mount: mount, segmentText: segmentText, isNewsViewKey: isNewsViewKey, makeFetcher: makeFetcher,
    isGen: isGen, genOf: genOf, researchGenOf: researchGenOf, startLease: startLease, tombstoneState: tombstoneState,
    VIEW_SCHEMA: VIEW_SCHEMA, INDEX_SCHEMA: INDEX_SCHEMA, INDEX_KEY: INDEX_KEY, MODES: MODES, STREAMS: STREAMS, MAX_PAGES: MAX_PAGES,
    LEASE_MS: LEASE_MS, DEADLINE_MS: DEADLINE_MS, CLOCK_AGREE_MS: CLOCK_AGREE_MS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NewsPanel = api;
})(typeof window !== 'undefined' ? window : this);

// Site wiring: the News tab calls this once, on first open (tab-switching.js).
// NEWS_DATA is the page's News base (index.html): the bounded /browser-data proxy wherever the page
// uses it, otherwise the dedicated public News origin (null until provisioned; never the shared
// market origin).
function loadNewsPanel() {
  // MediaFlow R1: the research view (js/news-research.js) follows this mount's index: it shows the
  // research page that index names, from the same generation, under the same lease.
  var box = document.getElementById('news-research');
  var onIndex = box && typeof NewsResearch !== 'undefined' ? NewsResearch.follow(document, box) : null;
  return NewsPanel.mount(document, document.getElementById('news-panel'),
    NewsPanel.makeFetcher(typeof NEWS_DATA === 'undefined' ? null : NEWS_DATA,
      function (url, opts) { return fetch(url, opts); }, Date.now),
    NewsPanel.INDEX_KEY, { events: typeof window !== 'undefined' ? window : null, onIndex: onIndex });
}
