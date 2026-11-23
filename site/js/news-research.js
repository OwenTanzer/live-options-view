// News research view (MediaFlow P1; R1 generation pages): renders the sanitized research projection
// (market_data/news/research.py, schema news-research-view/v1) that each pilot build publishes as one page
// of its generation, news/v2/views/p/<gen>.<n>/research/view.json, named by the index's "research" entry.
// site/index.html loads it before news-panel.js; loadNewsPanel passes follow(...) to NewsPanel.mount as
// onIndex, so this view reads only what the panel's one index read names (same generation, same lease)
// and has no key of its own. The view is FIXED: research mode at the build cutoff; nothing here chooses
// another mode or cutoff. The tests-only harness (tests/news_panel/research.html) still renders a
// projection directly.
// Text goes through textContent only; links are re-checked with new URL() and must be http(s).
// What the view keeps apart, and this page shows apart:
//   - how we know: evidence kind, the source chain (who published, who they cite, the origin) and each
//     hop's access result (read, http_403, login_required, not attempted...);
//   - what is reported: one subject and one action (with its literal status) per claim, quantities each
//     with what they are OF and when they were measured (often "unknown");
//   - context figures, which are not pressure quantities;
//   - a hypothesis, labelled CONTEMPORANEOUS only if it was made and committed before its horizon,
//     otherwise RETROSPECTIVE (never a prediction);
//   - every time with its kind and precision: publication / EDGAR acceptance / sent, our access, the
//     capture commit, the annotation time and its commit; and each record's revision provenance.
// The view is research records, not proof that any market claim or thesis is true; it says so.
(function (root) {
  'use strict';

  var VIEW_SCHEMA = 'news-research-view/v1';
  var MODES = ['collector', 'archive', 'publication', 'research'];
  var EVIDENCE = {
    primary_disclosure: 'primary disclosure', attributed_report: 'attributed report',
    model_estimate: 'model estimate', mechanism_hypothesis: 'mechanism hypothesis',
    thesis_assertion: 'thesis assertion'
  };
  var ACCESS = {
    read: 'read', http_401: 'HTTP 401 (login)', http_403: 'HTTP 403', bot_blocked: 'bot-blocked',
    login_required: 'login required', not_attempted: 'not attempted', not_available: 'not available to us'
  };
  var CSS = [
    '#news-research { max-width: 1100px; display: flex; flex-direction: column; gap: 0.6rem; padding: 0.75rem 1rem;',
    '  font-size: 0.85rem; line-height: 1.4; }',
    '#news-research .nr-box { background: var(--panel, #161b22); border: 1px solid var(--border, #30363d);',
    '  border-radius: 6px; padding: 0.6rem 0.75rem; min-width: 0; }',
    '#news-research .nr-head { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: baseline; }',
    '#news-research .nr-badge { display: inline-block; font-size: 0.7rem; font-weight: 600; padding: 1px 6px;',
    '  border: 1px solid var(--border, #30363d); border-radius: 10px; color: var(--fg, #c9d1d9); white-space: nowrap; }',
    '#news-research .nr-research { color: #4a90d9; border-color: #4a90d9; }',
    '#news-research .nr-retro { color: #ffaa88; border-color: #881100; }',
    '#news-research .nr-contemp { color: #00cc55; border-color: #00cc55; }',
    '#news-research .nr-synthetic { color: #d29922; border-color: #d29922; }',
    '#news-research .nr-warn { color: #d29922; border-color: #d29922; }',
    '#news-research .nr-bad { color: #ee3300; border-color: #ee3300; }',
    '#news-research .nr-dim, #news-research .nr-note { color: var(--dim, #6e7681); }',
    '#news-research .nr-note { font-size: 0.75rem; margin: 0.2rem 0; }',
    '#news-research .nr-title { color: var(--bright, #f0f6fc); font-weight: 600; overflow-wrap: anywhere; }',
    '#news-research a { color: #58a6ff; }',
    '#news-research a.nr-title { color: var(--bright, #f0f6fc); }',
    '#news-research a:focus-visible { outline: 2px solid #4a90d9; outline-offset: 1px; }',
    '#news-research .nr-times { list-style: none; padding: 0; margin: 0.3rem 0; font-size: 0.75rem;',
    '  display: flex; flex-direction: column; gap: 1px; }',
    '#news-research .nr-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }',
    '#news-research .nr-ann { border-left: 2px solid var(--border, #30363d); padding-left: 0.6rem; margin-top: 0.5rem; }',
    '#news-research .nr-claim { margin-top: 0.45rem; }',
    '#news-research .nr-claim h4 { font-size: 0.8rem; color: var(--bright, #f0f6fc); margin: 0;',
    '  display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: baseline; }',
    '#news-research ul.nr-list { margin: 0.2rem 0 0 1rem; padding: 0; font-size: 0.75rem; }',
    '#news-research .nr-hyp { margin-top: 0.45rem; padding: 0.4rem 0.5rem; border: 1px dashed var(--border, #30363d);',
    '  border-radius: 4px; }',
    '#news-research .nr-empty { color: var(--dim, #6e7681); font-style: italic; }',
    '#news-research .nr-btn { font: inherit; font-size: 0.8rem; padding: 2px 10px; cursor: pointer;',
    '  color: var(--fg, #c9d1d9); background: transparent; border: 1px solid var(--border, #30363d); border-radius: 4px; }',
    '@media (max-width: 600px) { #news-research { padding: 0.75rem 16px; } }'
  ].join('\n');

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
  function badge(doc, text, kind) { return el(doc, 'span', 'nr-badge' + (kind ? ' nr-' + kind : ''), text); }
  function short(id) { return id ? String(id).slice(0, 12) : 'none'; }

  function safeUrl(u) {
    if (typeof u !== 'string') return null;
    try {
      var p = new URL(u);
      return (p.protocol === 'https:' || p.protocol === 'http:') ? p.href : null;
    } catch (e) { return null; }
  }

  function ensureStyle(doc) {
    if (doc.getElementById && doc.getElementById('news-research-style')) return;
    var s = el(doc, 'style');
    s.id = 'news-research-style';
    s.textContent = CSS;
    (doc.head || doc.body).appendChild(s);
  }

  // A projection the page will render, or a list of problems (the page then shows a failure).
  function viewProblems(v) {
    var p = [];
    if (!v || typeof v !== 'object') return ['not an object'];
    if (v.schema_version !== VIEW_SCHEMA) p.push('schema_version is not ' + VIEW_SCHEMA);
    if (MODES.indexOf(v.mode) < 0) p.push('unknown mode');
    if (typeof v.cutoff !== 'string') p.push('cutoff missing');
    if (!Array.isArray(v.items)) p.push('items missing');
    else v.items.forEach(function (it, i) {
      if (!it || !it.source || !Array.isArray(it.annotations)) p.push('items[' + i + '] malformed');
      else it.annotations.forEach(function (a, j) {
        if (!a || !Array.isArray(a.claims) || typeof a.annotated_at !== 'string') p.push('items[' + i + '].annotations[' + j + '] malformed');
      });
    });
    return p;
  }

  function timeText(tv, fallback) {
    if (!tv || tv.value === 'unknown' || tv.value === null || tv.value === undefined) return fallback || 'unknown';
    return tv.value + (tv.precision && tv.precision !== 'second' ? ' (' + tv.precision + ' precision)' : '');
  }

  function pubLabel(kind) {
    return kind === 'edgar_acceptance' ? 'EDGAR acceptance' : kind === 'sent' ? 'sent' : 'published';
  }

  function accessText(acc) {
    var s = ACCESS[acc.result] || acc.result;
    if (acc.at && acc.at.value && acc.at.value !== 'unknown') s += ' at ' + timeText(acc.at);
    if (acc.attempts && acc.attempts.length) {
      s += ' (attempts: ' + acc.attempts.map(function (x) { return (ACCESS[x.result] || x.result) + ' ' + x.requested_at; }).join('; ') + ')';
    }
    return s;
  }

  function linkOrText(doc, text, url, cls) {
    var u = safeUrl(url);
    if (!u) return el(doc, 'span', cls, text);
    var a = el(doc, 'a', cls, text);
    a.href = u;
    a.rel = 'noopener noreferrer';
    a.target = '_blank';
    return a;
  }

  function renderSource(doc, it) {
    var s = it.source;
    var box = el(doc, 'div', 'nr-box');
    box.setAttribute('data-kind', s.kind);
    var head = add(el(doc, 'div', 'nr-head'),
      badge(doc, s.kind === 'research_capture' ? 'research capture' : 'collected', s.kind === 'research_capture' ? 'research' : null),
      linkOrText(doc, s.title || '(untitled)', s.url, 'nr-title'),
      el(doc, 'span', 'nr-dim', s.party || ''));
    var times = el(doc, 'ul', 'nr-times');
    add(times, el(doc, 'li', null, pubLabel(s.published && s.published.kind) + ' ' + timeText(s.published) +
      (s.published && s.published.basis ? ' · ' + s.published.basis : '')));
    if (s.updated) add(times, el(doc, 'li', null, 'updated ' + timeText(s.updated)));
    if (s.kind === 'research_capture') {
      add(times, el(doc, 'li', null, 'accessed ' + timeText(s.access.at) + ' — ' + accessText({ result: s.access.result, attempts: s.access.attempts }) +
        (s.access.via ? ' · via ' + s.access.via : '')));
      if (s.access.at && s.access.at.basis) add(times, el(doc, 'li', 'nr-dim', 'access time basis: ' + s.access.at.basis));
      add(times, el(doc, 'li', null, 'capture committed ' + (s.committed_at || 'not yet committed') + ' · no collector known-at'));
    } else {
      add(times, el(doc, 'li', null, 'observed by the collector ' + s.observed_at + ' · archived ' + (s.committed_at || 'not yet')));
    }
    add(times, el(doc, 'li', 'nr-mono nr-dim', (s.kind === 'research_capture' ? 'capture revision ' : 'revision ') +
      it.revision_count + ' · ' + short(s.revision_id) + (s.previous_revision_id ? ' ← ' + short(s.previous_revision_id) : ' (first)') +
      ' · item ' + short(s.item_id) + ' · run ' + (s.run_id || 'none')));
    add(box, head, times);
    if (s.note) add(box, el(doc, 'p', 'nr-note', s.note));
    if (!it.annotations.length) add(box, el(doc, 'p', 'nr-empty', 'no annotation visible at this cutoff'));
    it.annotations.forEach(function (a) { add(box, renderAnnotation(doc, a)); });
    if (it.annotations_on_other_revisions && it.annotations_on_other_revisions.length) {
      add(box, el(doc, 'p', 'nr-note', 'annotations made on an earlier revision: ' + it.annotations_on_other_revisions.join(', ') +
        ' (not carried over: a changed source needs a new annotation)'));
    }
    return box;
  }

  function renderAnnotation(doc, a) {
    var box = el(doc, 'div', 'nr-ann');
    box.setAttribute('data-lineage', a.lineage);
    var head = add(el(doc, 'div', 'nr-head'),
      el(doc, 'strong', null, 'annotation ' + a.lineage),
      badge(doc, a.method + ' ' + a.version),
      a.revision_count > 1 ? badge(doc, 'revision ' + a.revision_count + ' of ' + a.revision_count) : null,
      a.made_after_cutoff ? badge(doc, 'made after cutoff', 'retro') : null);
    var times = add(el(doc, 'ul', 'nr-times'),
      el(doc, 'li', null, 'made ' + a.annotated_at + ' · committed ' + (a.committed_at || 'not yet committed')),
      el(doc, 'li', 'nr-mono nr-dim', 'annotation ' + short(a.annotation_id) +
        (a.previous && a.previous.length ? ' ← ' + a.previous.map(short).join(' ← ') : '') +
        ' · on revision ' + short(a.input_revision_id) + ' · run ' + a.run_id));
    add(box, head, times);
    a.claims.forEach(function (c) { add(box, renderClaim(doc, c)); });
    if (a.context && a.context.length) {
      var ctx = add(el(doc, 'div', 'nr-claim'), el(doc, 'h4', null, 'Context (stated figures, not pressure quantities)'));
      add(ctx, list(doc, a.context.map(quantityText)));
      add(box, ctx);
    }
    if (a.hypothesis) add(box, renderHypothesis(doc, a.hypothesis));
    if (a.hypothesis_withheld) add(box, el(doc, 'p', 'nr-note', 'hypothesis withheld: ' + a.hypothesis_withheld));
    if (a.note) add(box, el(doc, 'p', 'nr-note', a.note));
    return box;
  }

  function quantityText(q) {
    return String(q.value) + ' ' + q.unit + ' — of ' + q.of + ' · measured ' + q.measured_at + ' · ' + q.basis;
  }

  function list(doc, lines) {
    var ul = el(doc, 'ul', 'nr-list');
    lines.forEach(function (l) { add(ul, el(doc, 'li', null, l)); });
    return ul;
  }

  function renderClaim(doc, c) {
    var box = el(doc, 'div', 'nr-claim');
    box.setAttribute('data-claim', c.id);
    var status = c.action.status;
    add(box, add(el(doc, 'h4'),
      el(doc, 'span', null, c.id),
      badge(doc, EVIDENCE[c.evidence_kind] || c.evidence_kind,
        c.evidence_kind === 'thesis_assertion' || c.evidence_kind === 'mechanism_hypothesis' ? 'warn' : null),
      el(doc, 'span', null, (c.subject.party || c.subject.population) + (c.subject.population ? ' (population)' : '')),
      el(doc, 'span', 'nr-dim', '·'),
      el(doc, 'span', null, c.action.kind),
      badge(doc, status, status === 'unverified' || status === 'contradicted' ? 'bad' : status === 'offered' ? 'warn' : null)));
    var lines = [];
    if (c.action.detail) lines.push('action: ' + c.action.detail);
    if (c.exposure) {
      lines.push('exposure: ' + c.exposure.direction + (c.exposure.to ? ' · ' + c.exposure.to : '') +
        (c.exposure.instrument_ids && c.exposure.instrument_ids.length ? ' · ' + c.exposure.instrument_ids.join(', ') : ''));
    }
    c.quantities.forEach(function (q) { lines.push('quantity: ' + quantityText(q)); });
    if (!c.quantities.length) lines.push('quantity: none stated');
    c.dates.forEach(function (d) { lines.push('date: ' + d.kind + ' ' + timeText(d.at) + ' (not a measurement time)'); });
    if (lines.length) add(box, list(doc, lines));
    var chain = el(doc, 'ul', 'nr-list');
    c.chain.forEach(function (h) {
      var li = el(doc, 'li');
      add(li, el(doc, 'span', null, h.role + ': '), linkOrText(doc, h.party, h.url),
        el(doc, 'span', 'nr-dim', ' — ' + accessText(h.access) + (h.revision ? ' · captured revision ' + short(h.revision.revision_id) : ' · not captured')),
        h.note ? el(doc, 'span', 'nr-dim', ' · ' + h.note) : null);
      add(chain, li);
    });
    add(box, el(doc, 'div', 'nr-note', 'source chain (what we read → who they cite → origin):'), chain);
    if (c.note) add(box, el(doc, 'p', 'nr-note', c.note));
    return box;
  }

  function renderHypothesis(doc, h) {
    var box = el(doc, 'div', 'nr-hyp');
    var retro = h.timing !== 'contemporaneous';
    add(box, add(el(doc, 'div', 'nr-head'),
      badge(doc, retro ? 'RETROSPECTIVE hypothesis' : 'CONTEMPORANEOUS hypothesis', retro ? 'retro' : 'contemp'),
      el(doc, 'span', 'nr-dim', retro ? 'made after its horizon began: not a prediction'
        : 'made and committed before its horizon began')));
    add(box, el(doc, 'p', null, h.statement));
    add(box, list(doc, [
      'horizon ' + timeText(h.horizon.start) + ' → ' + timeText(h.horizon.end) +
        (h.instruments && h.instruments.length ? ' · ' + h.instruments.join(', ') : ''),
      'disconfirmed by: ' + h.disconfirmation
    ].concat((h.missing_evidence || []).map(function (m) { return 'missing evidence: ' + m; }))));
    return box;
  }

  // Render a projection into container. Returns the data-state set on the container.
  function render(doc, container, view) {
    ensureStyle(doc);
    clear(container);
    var problems = viewProblems(view);
    if (problems.length) {
      container.setAttribute('data-state', 'failed');
      add(container, el(doc, 'p', 'nr-box nr-bad', 'Could not show research records: ' + problems.slice(0, 3).join('; ')));
      return 'failed';
    }
    var head = el(doc, 'div', 'nr-box');
    add(head, add(el(doc, 'div', 'nr-head'),
      badge(doc, view.mode === 'research' ? 'RESEARCH' : view.mode === 'publication' ? 'RETROSPECTIVE' : view.mode,
        view.mode === 'publication' ? 'retro' : 'research'),
      /SYNTHETIC/.test(view.data_label || '') ? badge(doc, 'SYNTHETIC', 'synthetic') : null,
      /PREPARED/.test(view.data_label || '') ? badge(doc, 'PREPARED', 'warn') : null,
      el(doc, 'span', null, view.label),
      el(doc, 'span', 'nr-dim', 'cutoff ' + view.cutoff)));
    add(head, el(doc, 'p', 'nr-note', view.data_label || ''));
    add(head, el(doc, 'p', 'nr-note', view.not_proof || 'Research records, not proof.'));
    var ex = view.excluded || {};
    if (ex.no_publication_time || ex.not_yet_visible) {
      add(head, el(doc, 'p', 'nr-note', 'not shown: ' + (ex.not_yet_visible || 0) + ' not yet visible at this cutoff, ' +
        (ex.no_publication_time || 0) + ' with no publication time (never guessed)'));
    }
    add(container, head);
    if (!view.items.length) {
      add(container, el(doc, 'p', 'nr-box nr-empty', 'No research records visible at this cutoff in ' + view.mode + ' mode.'));
    }
    view.items.forEach(function (it) { add(container, renderSource(doc, it)); });
    container.setAttribute('data-state', 'ready');
    container.setAttribute('data-items', String(view.items.length));
    return 'ready';
  }

  // MediaFlow R1: the research view follows the News panel's index (NewsPanel.mount's onIndex). The page is
  // the one the index names in its "research" entry, inside the same incarnation; it is fetched with the
  // panel's fetcher (the same exact-key check, deadline, no-store and ?_=) and shown only while the
  // panel's index lease holds. There is no research key of its own: the mutable
  // news/v2/research/view.json grant is retired, and the private ledger is never requested.
  var RESEARCH_PAGE = /^news\/v2\/views\/p\/([0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.[1-9][0-9]{0,3})\/research\/view\.json$/;
  var FIXED_NOTE = 'Fixed view: research mode at this publication\'s build time. No other mode or cutoff is published.';

  function entryProblems(entry, index) {
    var p = [];
    if (!entry || typeof entry !== 'object') return ['research entry is not an object'];
    if (entry.schema_version !== VIEW_SCHEMA) p.push('research entry schema is not ' + VIEW_SCHEMA);
    if (entry.mode !== 'research' || entry.fixed !== true) p.push('research entry is not the fixed research view');
    if (entry.cutoff !== index.build_as_of) p.push('research cutoff is not the build cutoff');
    if (['published', 'none', 'omitted'].indexOf(entry.state) < 0) p.push('unknown research state');
    if (entry.state === 'published') {
      var m = RESEARCH_PAGE.exec(entry.page);
      if (!m || m[1] !== index.gen) p.push('research page is not in generation ' + index.gen);
    } else if (entry.page !== null && entry.page !== undefined) p.push('research entry names a page in state ' + entry.state);
    return p;
  }

  function message(doc, container, state, cls, text) {
    clear(container);
    container.setAttribute('data-state', state);
    container.removeAttribute('data-page');
    add(container, el(doc, 'p', 'nr-box ' + cls, text));
    return state;
  }

  // Returns onIndex(event) for NewsPanel.mount. Never throws; only the latest event may render.
  function follow(doc, container) {
    ensureStyle(doc);
    message(doc, container, 'loading', 'nr-dim', 'Loading research records…');
    var latest = 0;
    return function onIndex(event) {
      var ticket = ++latest;
      var st = event && event.state;
      if (st === 'refresh') {
        return message(doc, container, 'refresh-required', 'nr-bad',
          'This research view was replaced or has expired. Use the Reload button in the News view above (' + (event.reason || 'refresh required') + ').');
      }
      if (st === 'withdrawn') return message(doc, container, 'withdrawn', 'nr-empty', 'Research records are not currently published');
      if (st === 'unpublished' || st === 'absent') return message(doc, container, 'absent', 'nr-empty', 'No research view published');
      if (st !== 'published') return message(doc, container, 'unavailable', 'nr-bad', 'Research records unavailable: the news index could not be loaded');
      var index = event.index;
      var entry = index.research;
      if (entry === undefined || entry === null) return message(doc, container, 'absent', 'nr-empty', 'No research view published');
      var problems = entryProblems(entry, index);
      if (problems.length) return message(doc, container, 'failed', 'nr-bad', 'Could not show research records: ' + problems.slice(0, 3).join('; '));
      var unverified = entry.unverified_runs ? entry.unverified_runs + ' research run(s) failed verification and are not shown. ' : '';
      if (entry.state === 'none') {
        return message(doc, container, 'absent', 'nr-empty', unverified + 'No research view published: no verified research run at this build');
      }
      if (entry.state === 'omitted') {
        return message(doc, container, 'omitted', 'nr-bad', unverified + 'The research view was left out of this publication (' +
          (entry.reason || 'omitted') + (entry.bytes ? ', ' + entry.bytes + ' bytes' : '') + ')');
      }
      message(doc, container, 'loading', 'nr-dim', 'Loading research records…');
      container.setAttribute('data-pending', entry.page);
      return event.fetchJson(entry.page).then(function (view) {
        if (ticket !== latest) return null;
        container.removeAttribute('data-pending');
        if (!event.leaseValid()) return onStale('the index lease ended while the research view loaded');
        if (!view || view.gen !== index.gen || view.build_as_of !== index.build_as_of || view.cutoff !== entry.cutoff ||
            view.mode !== 'research') {
          return onStale('the research page is not from generation ' + index.gen);
        }
        var state = render(doc, container, view);
        if (state === 'ready') {
          var head = container.firstChild;
          var fixed = el(doc, 'p', 'nr-note nr-fixed', FIXED_NOTE + ' Cutoff ' + view.cutoff + '.');
          if (head) head.insertBefore(fixed, head.firstChild.nextSibling); else add(container, fixed);
          if (unverified) add(head, el(doc, 'p', 'nr-note', unverified));
          container.setAttribute('data-page', entry.page);
        }
        return state;
      }, function (e) {
        if (ticket !== latest) return null;
        container.removeAttribute('data-pending');
        if (e && (e.newsKind === 'gone' || e.newsKind === 'deadline')) return onStale(e.message);
        return message(doc, container, 'failed', 'nr-bad', 'Could not load research records: ' + (e && e.message || 'unknown error'));
      });
      // The news views may still be ready (no Reload of their own), so the recovery is offered here: the
      // panel's shared-index reload (event.reload) re-reads the one index for both, never a second index.
      function onStale(reason) {
        message(doc, container, 'refresh-required', 'nr-bad',
          'This research view was replaced or has expired (' + reason + ').');
        var box = container.firstChild;
        if (typeof event.reload === 'function') {
          var button = el(doc, 'button', 'nr-btn', 'Reload news and research');
          button.setAttribute('data-action', 'reload-shared-index');
          button.addEventListener('click', function () {
            // Disabled and cleared at once: this control fires once; the new mount's events take over.
            if (button.disabled) return;
            button.disabled = true;
            ++latest;   // nothing pending from the old index may render after this
            message(doc, container, 'loading', 'nr-dim', 'Reloading news and research…');
            event.reload();
          });
          add(box, el(doc, 'span', null, ' '), button);
        } else {
          add(box, el(doc, 'span', null, ' Reload the page to see the current publication.'));
        }
        return 'refresh-required';
      }
    };
  }

  var api = { VIEW_SCHEMA: VIEW_SCHEMA, CSS: CSS, render: render, viewProblems: viewProblems, safeUrl: safeUrl,
              follow: follow, entryProblems: entryProblems, FIXED_NOTE: FIXED_NOTE };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NewsResearch = api;
})(typeof window !== 'undefined' ? window : this);
