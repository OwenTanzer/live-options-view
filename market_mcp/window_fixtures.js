'use strict';
// Synthetic #130 window fixtures (oa203-windows-v1), built from the real
// Oct 5-7 2026 replay shape in docs/130-acceptance-matrix.md. Never
// represented as acquired market data. Values reused from the published
// replay are the exact tops the matrix records: overnight Oct 5->6
// qualified rank 1 XLU261009C00040500 (pct 2.2, entry 0.15, exit 0.48,
// entry_ms 1791229787876, exit_ms 1791293734798), mid rank 1
// NBIS261009P00155000 (pct 33.33 in the matrix's 2dp display; the fixture
// uses the exact 6dp value 33.333333 from entry 0.03 / exit 1.03),
// exec rank 1 XLU261009C00041000 (pct 0.83 displayed; exact 0.833333 from
// entry 0.12 / exit 0.22), and sessions_3 Oct 5->7 qualified rank 1
// VST261009C00160000 (pct 13.38 displayed; exact 13.38 from entry 0.08 /
// exit 1.1504). Every other leg is invented to exercise the contract:
// different tops per basis, a wide-spread row eligible on mid/exec but
// excluded on qualified, a negative eligible row, and an
// expires-before-exit row. Session open/close ms are the real XNYS
// bounds for Oct 5-9 2026 (13:30-20:00 UTC).
//
// The weekly W41 fixture models the incomplete week as in_progress (Oct 8
// not final). The real replay skipped W41 outright; the fixture keeps the
// window published so the consumer's in_progress handling is exercised.
// The sessions_3 fixture carries the real replay's bounded_universe_replay
// partial reason: a replay-written final window is partial, never final.
const fs = require('node:fs/promises');
const path = require('node:path');
const { gzipSync } = require('node:zlib');
const { hash } = require('./source');

const POLICY = {
  version: 'oa203-windows-v1',
  decisions_status: 'D1-D5_D7_D8_approved; D6_D9_opening_tolerance_continuity_chej_engineering_reviewed_offline_only',
  early_week_sessions: 1, cheapest_entry_qualified: true, weekly_exit: 'last_session_close',
  overnight_across_gaps: true, multi_session_mode: 'rolling', multi_session_lengths: [2, 3],
  close_tolerance_s: 900, open_tolerance_s: 900, allow_expiration_day_exit: true,
  am_settled_roots: ['SPX', 'NDX', 'RUT', 'VIX'],
  qualified_endpoint_policy: { version: 'oa203-qualified-ask-bid-v1', min_entry_premium: 0.1,
    max_relative_spread: 0.3, max_quote_age_s: 1800, min_ask_size: 1, min_bid_size: 1,
    min_entry_volume: 1, min_samples: 3, min_coverage: 0.5, min_open_interest: null },
  qualification_scope: 'endpoint quote evidence only; intraday prefix checks not applied',
};
const D05 = { day: '2026-10-05', open_ms: 1791207000000, close_ms: 1791230400000 };
const D06 = { day: '2026-10-06', open_ms: 1791293400000, close_ms: 1791316800000 };
const D07 = { day: '2026-10-07', open_ms: 1791379800000, close_ms: 1791403200000 };
const D08 = { day: '2026-10-08', open_ms: 1791466200000, close_ms: 1791489600000 };
const D09 = { day: '2026-10-09', open_ms: 1791552600000, close_ms: 1791576000000 };
const COMPLETE = { final: true, assessment: 'complete', sweeps: 76 };
const PARTIAL_DAY = { final: true, assessment: 'partial', sweeps: 70 };
const NOT_FINAL_DAY = { final: false, assessment: 'partial', sweeps: 70 };
const BASES = ['mid', 'exec', 'qualified'];
const PATH_COLUMNS = ['symbol', 'underlying', 'root', 'option_type', 'strike', 'expiration',
  'contract_size', 'flags', 'contract_exclusions'];
const BASIS_COLUMNS = ['status', 'reasons', 'pct', 'entry', 'exit', 'abs_change_per_contract',
  'entry_ms', 'exit_ms', 'entry_session', 'exit_session', 'entry_delay_from_boundary_s',
  'exit_lead_before_close_s', 'entry_bid', 'entry_ask', 'entry_bid_ms', 'entry_ask_ms',
  'entry_ask_size', 'entry_bid_quote_age_s', 'entry_ask_quote_age_s', 'entry_quote_age_s',
  'entry_relative_spread', 'exit_bid', 'exit_ask', 'exit_bid_ms', 'exit_ask_ms', 'exit_bid_size',
  'exit_bid_quote_age_s', 'exit_ask_quote_age_s', 'exit_quote_age_s', 'exit_relative_spread'];
const RANKING_COLUMNS = ['basis', 'rank', 'symbol', 'underlying', 'option_type', 'strike',
  'expiration', 'pct', 'entry', 'exit', 'abs_change_per_contract', 'entry_session',
  'exit_session', 'flags'];

const round6 = v => Math.round(v * 1e6) / 1e6;
const round4 = v => Math.round(v * 1e4) / 1e4;
const esc = v => {
  const s = v === null || v === undefined ? '' : String(v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
};
// One eligible leg. bid/ask straddle entry/exit; for exec/qualified the
// entry ask is the entry and the exit bid is the exit, as the producer does.
function leg(entry, exit, entryMs, exitMs, entryBid, entryAsk, exitBid, exitAsk) {
  return { status: 'eligible', entry, exit, entry_ms: entryMs, exit_ms: exitMs,
    entry_bid: entryBid, entry_ask: entryAsk, exit_bid: exitBid, exit_ask: exitAsk,
    entry_bid_ms: entryMs - 90000, entry_ask_ms: entryMs - 60000,
    exit_bid_ms: exitMs - 120000, exit_ask_ms: exitMs - 60000,
    entry_ask_size: 5, exit_bid_size: 3 };
}
function excluded(reasons) { return { status: 'excluded', reasons }; }
function sessionOf(sessions, ms) { return sessions.find(s => s.open_ms <= ms && ms < s.close_ms); }

// Build one window's three files from compact contract specs. Counts,
// ranks, pct, sessions, delays, ages and spreads are derived exactly the
// way market_data/options_scanner/windows_outputs.py derives them, so the
// fixture is consistent by construction.
function buildWindow({ kind, window_id, sessions, entry_sessions, exit_session, state,
  state_reasons, scope, session_status, contracts, generated_at }) {
  const rows = contracts.map(c => {
    const row = { symbol: c.symbol, underlying: c.underlying, root: c.underlying,
      option_type: c.option_type, strike: c.strike, expiration: c.expiration,
      contract_size: 100, flags: c.flags || [], contract_exclusions: c.contract_exclusions || [] };
    for (const b of BASES) {
      const spec = c.legs[b];
      if (!spec || spec.status !== 'eligible') {
        row[b] = { status: 'excluded', reasons: spec ? spec.reasons : ['unknown'] };
        continue;
      }
      const entrySession = sessionOf(sessions, spec.entry_ms);
      const exitSession = sessionOf(sessions, spec.exit_ms);
      const pct = round6(spec.exit / spec.entry - 1);
      const bidAge = (spec.entry_ms - spec.entry_bid_ms) / 1000;
      const askAge = (spec.entry_ms - spec.entry_ask_ms) / 1000;
      const exitBidAge = (spec.exit_ms - spec.exit_bid_ms) / 1000;
      const exitAskAge = (spec.exit_ms - spec.exit_ask_ms) / 1000;
      const spread = (bid, ask) => round6(2 * (1 - bid / ask) / (1 + bid / ask));
      row[b] = { status: 'eligible', reasons: [], pct, entry: spec.entry, exit: spec.exit,
        abs_change_per_contract: round4((spec.exit - spec.entry) * 100),
        entry_ms: spec.entry_ms, exit_ms: spec.exit_ms,
        entry_session: entrySession.day, exit_session: exitSession.day,
        entry_delay_from_boundary_s: (spec.entry_ms - entrySession.open_ms) / 1000,
        exit_lead_before_close_s: (exitSession.close_ms - spec.exit_ms) / 1000,
        entry_bid: spec.entry_bid, entry_ask: spec.entry_ask,
        entry_bid_ms: spec.entry_bid_ms, entry_ask_ms: spec.entry_ask_ms,
        entry_ask_size: spec.entry_ask_size, entry_bid_quote_age_s: bidAge,
        entry_ask_quote_age_s: askAge, entry_quote_age_s: Math.max(bidAge, askAge),
        entry_relative_spread: spread(spec.entry_bid, spec.entry_ask),
        exit_bid: spec.exit_bid, exit_ask: spec.exit_ask,
        exit_bid_ms: spec.exit_bid_ms, exit_ask_ms: spec.exit_ask_ms,
        exit_bid_size: spec.exit_bid_size, exit_bid_quote_age_s: exitBidAge,
        exit_ask_quote_age_s: exitAskAge, exit_quote_age_s: Math.max(exitBidAge, exitAskAge),
        exit_relative_spread: spread(spec.exit_bid, spec.exit_ask) };
    }
    return row;
  });
  const counts = {};
  for (const b of BASES) {
    const eligible = rows.filter(r => r[b].status === 'eligible');
    const reasons = {};
    for (const r of rows) if (r[b].status !== 'eligible') {
      const key = (r[b].reasons || ['unknown'])[0].split(':')[0];
      reasons[key] = (reasons[key] ?? 0) + 1;
    }
    counts[b] = { eligible: eligible.length,
      negative: eligible.filter(r => r[b].pct < 0).length,
      excluded: rows.length - eligible.length,
      exclusion_reasons: Object.fromEntries(Object.entries(reasons).sort((x, y) => y[1] - x[1] || (x[0] < y[0] ? -1 : 1))),
      shortlist: Math.min(200, eligible.length) };
  }
  const ranked = [];
  for (const b of BASES) {
    const eligible = rows.filter(r => r[b].status === 'eligible')
      .sort((x, y) => y[b].pct - x[b].pct || (x.symbol < y.symbol ? -1 : 1));
    eligible.slice(0, 200).forEach((r, i) => ranked.push({ basis: b, rank: i + 1, symbol: r.symbol,
      underlying: r.underlying, option_type: r.option_type, strike: r.strike,
      expiration: r.expiration, pct: r[b].pct, entry: r[b].entry, exit: r[b].exit,
      abs_change_per_contract: r[b].abs_change_per_contract,
      entry_session: r[b].entry_session, exit_session: r[b].exit_session,
      flags: r.flags.join(';') }));
  }
  const rankingsCsv = RANKING_COLUMNS.join(',') + '\n' +
    ranked.map(r => RANKING_COLUMNS.map(c => esc(r[c])).join(',')).join('\n') + '\n';
  const header = [...PATH_COLUMNS, ...BASES.flatMap(b => BASIS_COLUMNS.map(c => `${b}_${c}`))];
  const pathsCsv = header.join(',') + '\n' + rows.slice().sort((a, b) => (a.symbol < b.symbol ? -1 : 1)).map(r => {
    const cells = [...PATH_COLUMNS.map(c => (c === 'flags' || c === 'contract_exclusions' ? r[c].join(';') : r[c]))];
    for (const b of BASES) for (const c of BASIS_COLUMNS) {
      const v = r[b][c];
      cells.push(Array.isArray(v) ? v.join(';') : (v === undefined ? '' : v));
    }
    return cells.map(esc).join(',');
  }).join('\n') + '\n';
  const labels = ['Sampled quotes (~5-minute sweeps); between-sample moves are missed.',
    'Quote comparisons, not executed fills; spread/size qualification does not establish fills, fees or capacity.',
    kind.startsWith('weekly_cheapest')
      ? 'Cheapest-entry and best-exit variants are hindsight results, not predictions.'
      : kind === 'weekly_fixed'
        ? 'Separate opening-price comparison; first qualifying sampled ask near the open.'
        : 'Fixed-endpoint window.'];
  const summary = { schema_version: 1, kind, window_id,
    sessions: sessions.map(s => ({ day: s.day, open_ms: s.open_ms, close_ms: s.close_ms,
      early_close: false, ...session_status[s.day] })),
    entry_sessions, exit_session, early_closes: [], state, state_reasons,
    policy: POLICY, generated_at, scope,
    calendar: 'XNYS (session bounds from each daily summary.json)',
    sources: sessions.map(s => ({ key: `oa203/scanner/${s.day}/summary.json`,
      sha256: hash(`window-fixture:${s.day}`) })),
    contracts_evaluated: rows.length, counts,
    outputs: { rankings: 'rankings.csv', paths: 'paths.csv.gz' }, labels };
  const prefix = `oa203/windows/oa203-windows-v1/${kind}/${window_id}/`;
  return { prefix, summary, rankingsCsv, pathsCsv, counts };
}

// The publisher writes one latest.json pointer per window (see
// market_data/options_scanner/windows_publish.py): schema_version 1,
// revision rev_<sha256(summary.json)>, member digests for all three files.
// Fixture windows carry a matching pointer so the consumer's revision-pin
// check is exercised on the happy path.
function pointerFor(w, summaryText, rankingsText, pathsGz) {
  return { schema_version: 1, publisher: 'windows_publish/1', policy: POLICY.version,
    kind: w.summary.kind, window_id: w.summary.window_id, state: w.summary.state,
    state_reasons: w.summary.state_reasons,
    revision: 'rev_' + hash(summaryText),
    members: { 'summary.json': hash(summaryText), 'rankings.csv': hash(rankingsText),
      'paths.csv.gz': hash(pathsGz) },
    generated_at: w.summary.generated_at, published_at: w.summary.generated_at };
}
function addPointer(files, w) {
  const summaryText = JSON.stringify(w.summary);
  const pathsGz = gzipSync(w.pathsCsv);
  files[w.prefix + 'summary.json'] = summaryText;
  files[w.prefix + 'rankings.csv'] = w.rankingsCsv;
  files[w.prefix + 'paths.csv.gz'] = pathsGz;
  files[w.prefix + 'latest.json'] = JSON.stringify(pointerFor(w, summaryText, w.rankingsCsv, pathsGz));
  return 'rev_' + hash(summaryText);
}

function windowFixtures() {
  const files = {};
  const json = (key, value) => { files[key] = JSON.stringify(value); };
  const windows = {};

  // Overnight Oct 5->6, final. Real replay tops per basis.
  const w1 = buildWindow({ kind: 'overnight', window_id: 'overnight-2026-10-05_2026-10-06',
    sessions: [D05, D06], entry_sessions: ['2026-10-05'], exit_session: '2026-10-06',
    state: 'final', state_reasons: [],
    scope: { mode: 'synthetic_fixture', note: 'final window as a live producer would write it; the offline replay instead labels final windows partial/bounded_universe_replay' },
    session_status: { '2026-10-05': COMPLETE, '2026-10-06': COMPLETE },
    generated_at: '2026-10-06T20:10:00Z',
    contracts: [
      { symbol: 'XLU261009C00040500', underlying: 'XLU', option_type: 'call', strike: 40.5, expiration: '2026-10-09', legs: {
        mid: leg(0.20, 0.50, 1791229600000, 1791293700000, 0.18, 0.22, 0.48, 0.52),
        exec: leg(0.16, 0.24, 1791229600000, 1791293700000, 0.14, 0.16, 0.24, 0.26),
        qualified: leg(0.15, 0.48, 1791229787876, 1791293734798, 0.13, 0.15, 0.48, 0.50) } },
      { symbol: 'XLU261009C00041000', underlying: 'XLU', option_type: 'call', strike: 41, expiration: '2026-10-09', legs: {
        mid: leg(0.25, 0.55, 1791229500000, 1791293760000, 0.23, 0.27, 0.53, 0.57),
        exec: leg(0.12, 0.22, 1791229500000, 1791293760000, 0.10, 0.12, 0.22, 0.24),
        qualified: leg(0.10, 0.17, 1791229500000, 1791293760000, 0.08, 0.10, 0.17, 0.19) } },
      { symbol: 'NBIS261009P00155000', underlying: 'NBIS', option_type: 'put', strike: 155, expiration: '2026-10-09', legs: {
        mid: leg(0.03, 1.03, 1791229400000, 1791293800000, 0.02, 0.04, 1.00, 1.06),
        exec: leg(0.04, 0.056, 1791229400000, 1791293800000, 0.03, 0.04, 0.056, 0.06),
        qualified: leg(0.05, 0.10, 1791229400000, 1791293800000, 0.04, 0.05, 0.10, 0.12) } },
      { symbol: 'QQQ261009C00600000', underlying: 'QQQ', option_type: 'call', strike: 600, expiration: '2026-10-09', legs: {
        mid: leg(0.40, 0.30, 1791229300000, 1791293900000, 0.38, 0.42, 0.28, 0.32),
        exec: leg(0.50, 0.30, 1791229300000, 1791293900000, 0.48, 0.50, 0.30, 0.32),
        qualified: leg(0.50, 0.45, 1791229300000, 1791293900000, 0.48, 0.50, 0.45, 0.47) } },
      { symbol: 'SPY261009C00670000', underlying: 'SPY', option_type: 'call', strike: 670, expiration: '2026-10-09', legs: {
        mid: leg(1.00, 1.60, 1791229200000, 1791294000000, 0.90, 1.10, 1.55, 1.65),
        exec: leg(1.10, 1.32, 1791229200000, 1791294000000, 0.90, 1.10, 1.32, 1.35),
        qualified: excluded(['entry_spread']) } },
      { symbol: 'XYZ261009C00010000', underlying: 'XYZ', option_type: 'call', strike: 10, expiration: '2026-10-05',
        contract_exclusions: ['expires_before_exit'], legs: {
        mid: excluded(['expires_before_exit']),
        exec: excluded(['expires_before_exit']),
        qualified: excluded(['expires_before_exit']) } },
    ] });
  const rev1 = addPointer(files, w1);
  windows.overnightFinal = { kind: 'overnight', window_id: 'overnight-2026-10-05_2026-10-06', prefix: w1.prefix, summary: w1.summary, counts: w1.counts, revision: rev1 };

  // Sessions_3 Oct 5->7, partial with the real replay's bounded reason.
  const w2 = buildWindow({ kind: 'sessions_3', window_id: 'sessions3-2026-10-05_2026-10-07',
    sessions: [D05, D06, D07], entry_sessions: ['2026-10-05'], exit_session: '2026-10-07',
    state: 'partial', state_reasons: ['bounded_universe_replay'],
    scope: { mode: 'offline_replay' },
    session_status: { '2026-10-05': COMPLETE, '2026-10-06': COMPLETE, '2026-10-07': COMPLETE },
    generated_at: '2026-10-07T20:10:00Z',
    contracts: [
      { symbol: 'VST261009C00160000', underlying: 'VST', option_type: 'call', strike: 160, expiration: '2026-10-09', legs: {
        mid: leg(0.10, 0.90, 1791229700000, 1791402000000, 0.08, 0.12, 0.88, 0.92),
        exec: leg(0.12, 0.62, 1791229700000, 1791402000000, 0.10, 0.12, 0.62, 0.64),
        qualified: leg(0.08, 1.1504, 1791229700000, 1791402000000, 0.06, 0.08, 1.1504, 1.17) } },
      { symbol: 'CEG261009C00280000', underlying: 'CEG', option_type: 'call', strike: 280, expiration: '2026-10-09', legs: {
        mid: leg(0.15, 2.00, 1791229600000, 1791402100000, 0.13, 0.17, 1.98, 2.02),
        exec: leg(0.18, 1.00, 1791229600000, 1791402100000, 0.16, 0.18, 1.00, 1.02),
        qualified: leg(0.20, 1.00, 1791229600000, 1791402100000, 0.18, 0.20, 1.00, 1.02) } },
      { symbol: 'NBIS261009P00155000', underlying: 'NBIS', option_type: 'put', strike: 155, expiration: '2026-10-09', legs: {
        mid: leg(0.05, 0.30, 1791229500000, 1791402200000, 0.04, 0.06, 0.28, 0.32),
        exec: leg(0.07, 0.10, 1791229500000, 1791402200000, 0.06, 0.07, 0.10, 0.12),
        qualified: leg(0.06, 0.12, 1791229500000, 1791402200000, 0.05, 0.06, 0.12, 0.14) } },
      { symbol: 'SPY261009C00670000', underlying: 'SPY', option_type: 'call', strike: 670, expiration: '2026-10-09', legs: {
        mid: leg(1.00, 1.20, 1791229400000, 1791402300000, 0.90, 1.10, 1.15, 1.25),
        exec: leg(1.10, 1.15, 1791229400000, 1791402300000, 0.90, 1.10, 1.15, 1.17),
        qualified: excluded(['entry_spread']) } },
    ] });
  const rev2 = addPointer(files, w2);
  windows.sessions3Partial = { kind: 'sessions_3', window_id: 'sessions3-2026-10-05_2026-10-07', prefix: w2.prefix, summary: w2.summary, counts: w2.counts, revision: rev2 };

  // Overnight Oct 7->8, partial: Oct 8's daily assessment is partial.
  const w3 = buildWindow({ kind: 'overnight', window_id: 'overnight-2026-10-07_2026-10-08',
    sessions: [D07, D08], entry_sessions: ['2026-10-07'], exit_session: '2026-10-08',
    state: 'partial', state_reasons: ['session_partial:2026-10-08'],
    scope: { mode: 'offline_replay' },
    session_status: { '2026-10-07': COMPLETE, '2026-10-08': PARTIAL_DAY },
    generated_at: '2026-10-08T20:10:00Z',
    contracts: [
      { symbol: 'XLU261009C00040500', underlying: 'XLU', option_type: 'call', strike: 40.5, expiration: '2026-10-09', legs: {
        mid: leg(0.20, 0.45, 1791402500000, 1791466500000, 0.18, 0.22, 0.43, 0.47),
        exec: leg(0.16, 0.28, 1791402500000, 1791466500000, 0.14, 0.16, 0.28, 0.30),
        qualified: leg(0.15, 0.30, 1791402500000, 1791466500000, 0.13, 0.15, 0.30, 0.32) } },
      { symbol: 'QQQ261009C00600000', underlying: 'QQQ', option_type: 'call', strike: 600, expiration: '2026-10-09', legs: {
        mid: leg(0.40, 0.35, 1791402400000, 1791466600000, 0.38, 0.42, 0.33, 0.37),
        exec: leg(0.50, 0.42, 1791402400000, 1791466600000, 0.48, 0.50, 0.42, 0.44),
        qualified: leg(0.50, 0.40, 1791402400000, 1791466600000, 0.48, 0.50, 0.40, 0.42) } },
      { symbol: 'SPY261009C00670000', underlying: 'SPY', option_type: 'call', strike: 670, expiration: '2026-10-09', legs: {
        mid: leg(1.00, 1.10, 1791402300000, 1791466700000, 0.90, 1.10, 1.05, 1.15),
        exec: leg(1.10, 1.12, 1791402300000, 1791466700000, 0.90, 1.10, 1.12, 1.14),
        qualified: excluded(['entry_spread']) } },
    ] });
  const rev3 = addPointer(files, w3);
  windows.overnightPartial = { kind: 'overnight', window_id: 'overnight-2026-10-07_2026-10-08', prefix: w3.prefix, summary: w3.summary, counts: w3.counts, revision: rev3 };

  // Weekly W41 cheapest, in_progress: Oct 8 has no final daily summary.
  const w4 = buildWindow({ kind: 'weekly_cheapest', window_id: 'weekly-2026-W41-cheapest',
    sessions: [D05, D06, D07, D08, D09], entry_sessions: ['2026-10-05'], exit_session: '2026-10-09',
    state: 'in_progress', state_reasons: ['session_not_final:2026-10-08'],
    scope: { mode: 'offline_replay' },
    session_status: { '2026-10-05': COMPLETE, '2026-10-06': COMPLETE, '2026-10-07': COMPLETE,
      '2026-10-08': NOT_FINAL_DAY, '2026-10-09': COMPLETE },
    generated_at: '2026-10-09T20:10:00Z',
    contracts: [
      { symbol: 'XLU261009C00040500', underlying: 'XLU', option_type: 'call', strike: 40.5, expiration: '2026-10-09', legs: {
        mid: leg(0.20, 0.70, 1791229787876, 1791575000000, 0.18, 0.22, 0.68, 0.72),
        exec: leg(0.16, 0.55, 1791229787876, 1791575000000, 0.14, 0.16, 0.55, 0.57),
        qualified: leg(0.15, 0.60, 1791229787876, 1791575000000, 0.13, 0.15, 0.60, 0.62) } },
      { symbol: 'QQQ261009C00600000', underlying: 'QQQ', option_type: 'call', strike: 600, expiration: '2026-10-09', legs: {
        mid: leg(0.40, 0.38, 1791229300000, 1791575100000, 0.38, 0.42, 0.36, 0.40),
        exec: leg(0.50, 0.48, 1791229300000, 1791575100000, 0.48, 0.50, 0.48, 0.50),
        qualified: leg(0.50, 0.45, 1791229300000, 1791575100000, 0.48, 0.50, 0.45, 0.47) } },
    ] });
  const rev4 = addPointer(files, w4);
  windows.weeklyInProgress = { kind: 'weekly_cheapest', window_id: 'weekly-2026-W41-cheapest', prefix: w4.prefix, summary: w4.summary, counts: w4.counts, revision: rev4 };

  // Unsupported producer policy: same v1 key path, summary claims v2.
  const unsupported = { schema_version: 1, kind: 'overnight', window_id: 'overnight-2026-10-06_2026-10-07',
    sessions: [ { day: '2026-10-06', open_ms: D06.open_ms, close_ms: D06.close_ms, early_close: false, ...COMPLETE },
      { day: '2026-10-07', open_ms: D07.open_ms, close_ms: D07.close_ms, early_close: false, ...COMPLETE } ],
    entry_sessions: ['2026-10-06'], exit_session: '2026-10-07', early_closes: [],
    state: 'final', state_reasons: [],
    policy: { ...POLICY, version: 'oa203-windows-v2' }, generated_at: '2026-10-07T20:10:00Z',
    scope: { mode: 'offline_replay' }, calendar: 'XNYS (session bounds from each daily summary.json)',
    sources: [], contracts_evaluated: 0,
    counts: { mid: { eligible: 0, negative: 0, excluded: 0, exclusion_reasons: {}, shortlist: 0 },
      exec: { eligible: 0, negative: 0, excluded: 0, exclusion_reasons: {}, shortlist: 0 },
      qualified: { eligible: 0, negative: 0, excluded: 0, exclusion_reasons: {}, shortlist: 0 } },
    outputs: { rankings: 'rankings.csv', paths: 'paths.csv.gz' },
    labels: ['Sampled quotes (~5-minute sweeps); between-sample moves are missed.',
      'Quote comparisons, not executed fills; spread/size qualification does not establish fills, fees or capacity.',
      'Fixed-endpoint window.'] };
  const unsupportedPrefix = 'oa203/windows/oa203-windows-v1/overnight/overnight-2026-10-06_2026-10-07/';
  json(unsupportedPrefix + 'summary.json', unsupported);
  windows.unsupportedPolicy = { kind: 'overnight', window_id: 'overnight-2026-10-06_2026-10-07', prefix: unsupportedPrefix, summary: unsupported };

  return { files, windows, POLICY };
}
async function writeWindowFixtures(root) {
  const { files } = windowFixtures();
  for (const [key, value] of Object.entries(files)) {
    const target = path.join(root, key);
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.writeFile(target, value);
  }
}
module.exports = { windowFixtures, writeWindowFixtures, POLICY };
