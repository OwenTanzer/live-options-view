// B3 slice 4 (Nov 2026): the panel status formatters (formatVwapRvol /
// formatMomentum, the short-squeeze scanner formatters, fmtSteoDelta /
// findRevision) and the order validators (normalizePaperOrder /
// normalizeShareOrder) moved verbatim out of site/shared.js into
// site/shared/format.js. Pins the module's ?v= hash in the page, asserts the
// ten functions and two constants no longer live in shared.js, pins the
// entry's export surface (the ten functions are still re-exported — the same
// function objects — while the two staleness constants stay browser globals
// by design), and drives the moved code directly.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'site', 'index.html'), 'utf8');
const shared = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared.js'), 'utf8');
const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'shared', 'format.js'), 'utf8');
const format = require('../site/shared/format.js');
const entry = require('../site/shared.js');

const moduleHash = createHash('sha256').update(moduleSource).digest('hex').slice(0, 16);
assert.ok(html.includes('shared/format.js?v=' + moduleHash),
  'format script URL must match its content hash on every hosting path');

const FUNCTIONS = ['formatVwapRvol', 'formatMomentum', 'formatSqueezeScanStatus', 'formatSqueezeScheduleStatus',
  'describeSqueezeAcquisitionFailure', 'formatSqueezeFirstSeen', 'fmtSteoDelta', 'findRevision',
  'normalizePaperOrder', 'normalizeShareOrder'];
for (const name of FUNCTIONS) {
  assert.ok(!shared.includes(`function ${name}(`), `${name} must no longer live in shared.js`);
  assert.equal(typeof format[name], 'function', `format.js must export ${name}`);
  assert.equal(entry[name], format[name], `the entry must re-export the moved ${name} itself`);
}
for (const decl of ['const VWAP_RVOL_CLOSED_AFTER_MS = 4 * 60 * 60 * 1000;',
  'const SQUEEZE_SCAN_STALE_AFTER_MS = 6 * 60 * 60 * 1000;']) {
  assert.ok(!shared.includes(decl), `${decl} must no longer live in shared.js`);
  assert.equal(entry[decl.slice(6, decl.indexOf(' ='))], undefined, 'a staleness constant stays a browser global');
}
assert.equal(format.VWAP_RVOL_CLOSED_AFTER_MS, 4 * 60 * 60 * 1000, 'VWAP closes 4h after the last observation');
assert.equal(format.SQUEEZE_SCAN_STALE_AFTER_MS, 6 * 60 * 60 * 1000, 'a scan goes stale after 6h');

// --- formatVwapRvol / formatMomentum: the same values the price strip renders. ---
assert.deepEqual(format.formatVwapRvol(null), { text: 'VWAP/RVOL: unavailable', state: 'fallback' });
assert.deepEqual(format.formatMomentum(null), { text: 'Momentum: unavailable', state: 'fallback' });
{
  const um = { spot_ts: '2026-09-18T20:00:00Z', vwap: 600.5, price_vs_vwap_pct: 0.4,
    rvol: { status: 'ok', multiple: 1.2 }, freshness: 'live' };
  assert.deepEqual(format.formatVwapRvol(um, Date.parse('2026-09-18T20:30:00Z')),
    { text: 'VWAP 600.50 (+0.40%) | RVOL 1.20×', state: 'live' });
  assert.deepEqual(format.formatVwapRvol(um, Date.parse('2026-09-19T01:00:00Z')),
    { text: 'VWAP/RVOL: session closed', state: 'closed' });
  assert.equal(format.formatVwapRvol({ ...um, rvol: { status: 'insufficient_history' } }, Date.parse('2026-09-18T20:30:00Z')).text,
    'VWAP 600.50 (+0.40%) | RVOL warming up');
  assert.equal(format.formatVwapRvol({ ...um, vwap: null }, Date.parse('2026-09-18T20:30:00Z')).text,
    'VWAP — | RVOL 1.20×');
  const mom = { spot_ts: '2026-09-18T20:00:00Z', freshness: 'live',
    momentum: { status: 'ok', return_pct: 0.4, direction: 'up', lookback_minutes: 30 } };
  assert.deepEqual(format.formatMomentum(mom, Date.parse('2026-09-18T20:30:00Z')),
    { text: 'Momentum ▲ +0.40% (30m)', state: 'live' });
  assert.equal(format.formatMomentum({ ...mom, momentum: { status: 'warming_up' } }, Date.parse('2026-09-18T20:30:00Z')).text,
    'Momentum warming up');
  assert.equal(format.formatMomentum({ ...mom, momentum: { status: 'stale_anchor' } }, Date.parse('2026-09-18T20:30:00Z')).text,
    'Momentum stale (gap in data)');
  assert.equal(format.formatMomentum({ ...mom, momentum: { status: 'other' } }, Date.parse('2026-09-18T20:30:00Z')).text,
    'Momentum —');
}

// --- fmtSteoDelta / findRevision: the STEO panel deltas. ---
assert.deepEqual(format.fmtSteoDelta(null), { text: '', cls: '' });
assert.deepEqual(format.fmtSteoDelta(2), { text: '+2.00', cls: 'up' });
assert.deepEqual(format.fmtSteoDelta(-1.5), { text: '-1.50', cls: 'down' });
assert.deepEqual(format.fmtSteoDelta(0), { text: '+0.00', cls: '' });
assert.equal(format.findRevision([{ period: '2026-10', brent_delta: 2 }], '2026-10').brent_delta, 2);
assert.equal(format.findRevision([{ period: '2026-10' }], '2026-11'), null);

// --- squeeze formatters: freshness is judged on acquisition time. ---
assert.deepEqual(format.formatSqueezeScanStatus(null), { text: 'No scan published yet', state: 'fallback' });
{
  const pointer = { status: 'complete', finished_at: '2026-09-23T13:00:13Z', started_at: '2026-09-23T13:00:03Z' };
  assert.deepEqual(format.formatSqueezeScanStatus(pointer, Date.parse('2026-09-23T14:00:00Z')),
    { text: 'Scan 9:00 AM ET', state: 'live' });
  assert.deepEqual(format.formatSqueezeScanStatus(pointer, Date.parse('2026-09-23T20:30:00Z')),
    { text: 'Scan 9:00 AM ET (stale)', state: 'stale' });
  assert.deepEqual(format.formatSqueezeScanStatus({ ...pointer, status: 'empty' }, Date.parse('2026-09-23T14:00:00Z')),
    { text: 'Scan complete 9:00 AM ET — no eligible candidates', state: 'stale' });
  const bad = format.formatSqueezeScheduleStatus(null, null, null, null, Date.parse('2026-09-23T14:00:00Z'));
  assert.equal(bad.text, 'No scan published yet');
  assert.equal(bad.state, 'stale');
  assert.equal(bad.warning, 'Schedule calendar unavailable or outside its coverage. Freshness is unconfirmed.');
  assert.equal(format.describeSqueezeAcquisitionFailure(null, null), null);
  assert.equal(format.describeSqueezeAcquisitionFailure(pointer, { ...pointer, status: 'complete' }), null);
  const failed = format.describeSqueezeAcquisitionFailure(pointer,
    { status: 'failed', started_at: '2026-09-23T17:00:00Z' });
  assert.match(failed, /did not complete: failed/);
  assert.equal(format.describeSqueezeAcquisitionFailure(pointer,
    { status: 'failed', started_at: '2026-09-23T12:00:00Z' }), null, 'an older failure stays invisible');
  assert.deepEqual(format.formatSqueezeFirstSeen({ first_seen_at: null, is_new: null }),
    { text: 'unavailable', isNew: false });
  assert.deepEqual(format.formatSqueezeFirstSeen({ first_seen_at: '2026-09-23T13:00:56Z', is_new: true }),
    { text: '9:00 AM', isNew: true });
  assert.deepEqual(format.formatSqueezeFirstSeen({ first_seen_at: '2026-09-23T13:00:56Z', is_new: false }),
    { text: '9:00 AM', isNew: false });
}

// --- order validation: the trade ticket's gate. ---
assert.deepEqual(format.normalizePaperOrder({ side: 'buy', quantity: 10 }),
  { value: { side: 'buy', qty: 10, order_type: 'market' } });
assert.deepEqual(format.normalizePaperOrder({ side: 'buy', quantity: 2, orderType: 'limit', limitPrice: 10.25 }),
  { value: { side: 'buy', qty: 2, order_type: 'limit', limit_price: 10.25 } });
assert.equal(format.normalizePaperOrder({ side: 'hold', quantity: 1 }).error, 'Choose buy or sell.');
assert.equal(format.normalizePaperOrder({ side: 'buy', quantity: 0 }).error, 'Quantity must be a positive whole number.');
assert.equal(format.normalizePaperOrder({ side: 'buy', quantity: 1, orderType: 'limit', limitPrice: 0 }).error,
  'Limit price must be greater than zero.');
assert.equal(format.normalizePaperOrder({ side: 'buy', quantity: 1, orderType: 'limit', limitPrice: 10.005 }).error,
  'Limit price must use increments of $0.01.');
assert.equal(format.normalizeShareOrder({ side: 'sell', quantity: 5, heldQuantity: 2 }).error,
  'You cannot sell more shares than you hold.');
assert.deepEqual(format.normalizeShareOrder({ side: 'sell', quantity: 2, heldQuantity: 2 }),
  { value: { side: 'sell', qty: 2, order_type: 'market' } });

// --- #178: the chain header's snapshot line (session, price age, retention). ---
// The session comes from the producer's own ET stamp, so a reader in another
// time zone cannot relabel it; the age comes from the same `timestamp` the
// status dot judges.
assert.equal(format.chainSessionFromEtTime('09:29 ET'), 'premarket');
assert.equal(format.chainSessionFromEtTime('09:30 ET'), 'regular');
assert.equal(format.chainSessionFromEtTime('15:59 ET'), 'regular');
assert.equal(format.chainSessionFromEtTime('16:00 ET'), 'afterhours');
assert.equal(format.chainSessionFromEtTime('16:14 ET'), 'afterhours');
assert.equal(format.chainSessionFromEtTime('19:59 ET'), 'afterhours');
assert.equal(format.chainSessionFromEtTime('20:00 ET'), 'overnight');
assert.equal(format.chainSessionFromEtTime('04:00 ET'), 'premarket');
assert.equal(format.chainSessionFromEtTime('12:00'), null, 'no ET stamp means no session claim');
assert.equal(format.chainSessionFromEtTime(''), null);
assert.equal(format.chainSessionFromEtTime(null), null);
assert.equal(format.formatSnapshotAge(30_000), 'just now');
assert.equal(format.formatSnapshotAge(12 * 60_000), '12m ago');
assert.equal(format.formatSnapshotAge(7 * 3_600_000 + 3 * 60_000), '7h 3m ago');
assert.equal(format.formatSnapshotAge(2 * 3_600_000), '2h ago');
assert.equal(format.formatSnapshotAge(26 * 3_600_000), '1d 2h ago');
assert.equal(format.formatSnapshotAge(NaN), 'age unknown');

const CHAIN_SNAPSHOT = {
  symbol: 'QQQ', snapshot_time: '16:14 ET', timestamp: '11/10/2026 13:14:45',
  date: '2026-11-10', tier: '0DTE_Regular', underlying_price: 675.34,
};
// `timestamp` is the collector's Pacific wall clock beside an ET label, so the
// instant must not depend on the reader's zone: 13:14:45 PST is 21:14:45Z.
const SNAP_MS = Date.UTC(2026, 10, 10, 21, 14, 45);
assert.equal(format.chainSnapshotInstant(CHAIN_SNAPSHOT), SNAP_MS,
  'the producer clock is read as Pacific, not as browser-local time');
assert.equal(format.chainSnapshotInstant({ ...CHAIN_SNAPSHOT, timestamp: 'nonsense' }), Date.UTC(2026, 10, 10, 21, 14, 0),
  'without a parseable timestamp the ET pair (date + snapshot_time) still fixes the instant, to the minute');
assert.equal(format.chainSnapshotInstant({}), null, 'no evidence, no instant');
assert.equal(format.chainSnapshotInstant({
  ...CHAIN_SNAPSHOT,
  underlying_market: { spot_ts: '2026-11-10T13:14:48.084735-08:00' },
}), Date.parse('2026-11-10T13:14:48.084735-08:00'), 'an offset-bearing spot timestamp wins: it carries its own zone');
// Decisive zone independence: the same page code run under a Tokyo clock must
// print the same header line as under a Pacific one.
{
  const { execFileSync } = require('node:child_process');
  const script = `
    const f = require(${JSON.stringify(require.resolve('../site/shared/format.js'))});
    process.stdout.write(f.formatChainSnapshot(${JSON.stringify(CHAIN_SNAPSHOT)}, ${SNAP_MS + 7_200_000}).text);`;
  const tokyo = execFileSync(process.execPath, ['-e', script], { encoding: 'utf8', env: { ...process.env, TZ: 'Asia/Tokyo' } });
  const pacific = execFileSync(process.execPath, ['-e', script], { encoding: 'utf8', env: { ...process.env, TZ: 'America/Los_Angeles' } });
  assert.equal(tokyo, 'snapshot 16:14 ET · After hours · 2h ago · retained');
  assert.equal(tokyo, pacific, 'the header age is identical in every reader time zone');
}
{
  // The live-case #178 mismatch: a chain captured at 16:14 ET, read hours
  // later, must say so rather than looking like a current price.
  const f = format.formatChainSnapshot(CHAIN_SNAPSHOT, SNAP_MS + 3_600_000);
  assert.equal(f.state, 'retained', 'an hour-old chain snapshot is a retained reading');
  assert.equal(f.session, 'afterhours', '"Regular" is the tier, not the session: 16:14 ET is after hours');
  assert.equal(f.sessionLabel, 'After hours');
  assert.equal(f.ageMs, 3_600_000);
  assert.equal(f.text, 'snapshot 16:14 ET · After hours · 1h ago · retained');
  assert.match(f.detail, /historical snapshot taken at 16:14 ET during the after hours, 1h ago/);
  assert.match(f.detail, /not current prices/);
  assert.match(f.detail, /Core markets shows live prices/);

  const live = format.formatChainSnapshot(CHAIN_SNAPSHOT, SNAP_MS + 20_000);
  assert.equal(live.state, 'live', 'a 20-second-old snapshot is live');
  assert.equal(live.text, 'snapshot 16:14 ET · After hours · just now');
  assert.match(live.detail, /Chain snapshot taken at 16:14 ET during the after hours, just now/);

  const middle = format.formatChainSnapshot({ ...CHAIN_SNAPSHOT, snapshot_time: '11:42 ET' }, SNAP_MS + 3_600_000);
  assert.equal(middle.session, 'regular', '11:42 ET is the regular session');
  assert.equal(middle.text, 'snapshot 11:42 ET · Regular session · 1h ago · retained');

  const exactly = format.formatChainSnapshot(CHAIN_SNAPSHOT, SNAP_MS + format.CHAIN_SNAPSHOT_LIVE_MS);
  assert.equal(exactly.state, 'live', 'the live bound is the same 2 minutes the status dot uses');
  assert.equal(format.formatChainSnapshot(CHAIN_SNAPSHOT, SNAP_MS + format.CHAIN_SNAPSHOT_LIVE_MS + 1).state, 'retained');

  // A payload whose wall clock is unusable still has its ET label, so the age
  // is minute-accurate rather than absent.
  const etOnly = format.formatChainSnapshot({ ...CHAIN_SNAPSHOT, timestamp: 'not a time' }, SNAP_MS);
  assert.equal(etOnly.state, 'live');
  assert.equal(etOnly.ageMs, 45_000, 'the ET label is used, to the minute, when the clock string is junk');

  const untimed = format.formatChainSnapshot({ ...CHAIN_SNAPSHOT, timestamp: 'not a time', date: undefined, snapshot_time: undefined }, SNAP_MS);
  assert.equal(untimed.state, 'untimed', 'with no usable time evidence at all the age is reported, never guessed');
  assert.equal(untimed.ageMs, null);
  assert.match(untimed.text, /age unknown$/);
  assert.equal(format.formatChainSnapshot({}, SNAP_MS).state, 'untimed', 'no snapshot at all is untimed');
  assert.match(format.formatChainSnapshot({}, SNAP_MS).text, /snapshot time unknown/);
}
console.log('format: vwap/momentum/squeeze/STEO/order checks and the #178 chain snapshot line passed');
