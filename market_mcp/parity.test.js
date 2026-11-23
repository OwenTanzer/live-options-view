'use strict';
// Producer-publisher-consumer parity bridge (F2).
//
// The Python parity test (tests/test_oa203_windows_parity.py) builds every
// supported window kind with the REAL producer, publishes each with the REAL
// publisher to a LocalFilesystemBackend tree, and writes a manifest of the
// producer's own rows/counts/detail. This test reads that manifest and the
// same publisher-written trees through the REAL consumer over a local
// Source directory, asserting the consumer serves exactly what the producer
// wrote: same rows, ranks, pcts, entry/exit and pinned detail, for every
// kind x basis x state. It is skipped unless PARITY_MANIFEST is set, so the
// ordinary `node --test market_mcp/*.test.js` run stays hermetic.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { Consumer } = require('./consumer');
const { Source } = require('./source');

const manifestPath = process.env.PARITY_MANIFEST;
const BASES = ['mid', 'exec', 'qualified'];
const ROW_FIELDS = ['symbol', 'rank', 'pct', 'entry', 'exit', 'underlying', 'option_type',
  'strike', 'expiration', 'entry_session', 'exit_session'];
const DETAIL_FIELDS = ['pct', 'entry', 'exit', 'entry_ms', 'exit_ms', 'entry_session', 'exit_session'];
const NOW = Date.parse('2026-10-10T12:00:00Z');

test('parity: producer-publisher-consumer for every kind, basis and state',
  { skip: !manifestPath }, async () => {
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    assert.equal(manifest.version, 1);
    assert.ok(Array.isArray(manifest.variants) && manifest.variants.length > 0);
    const seen = new Set();
    for (const v of manifest.variants) {
      const id = `${v.kind}/${v.window_id}/${v.state}`;
      assert.ok(!seen.has(id), `duplicate manifest variant ${id}`);
      seen.add(id);
      const source = new Source({ directory: v.root });
      const consumer = new Consumer(source, { now: () => NOW });
      for (const basis of BASES) {
        // Page through every row: the per-call limit is 50, and a
        // full-universe window carries 200 rows per basis. The first page
        // carries the status/coverage/summary assertions and the rank-1
        // detail check; the concatenated pages must equal the producer's
        // full shortlist. The detail check runs on the first page BEFORE
        // paging on: window() retains one reference per returned row and
        // the store holds 64 live references, so paging 200 rows evicts
        // page 1's rank-1 reference by design (the detail error says to
        // repeat the originating query). A client must fetch a row's
        // detail while its reference is live.
        const rows = [];
        let offset = 0;
        let out = null;
        const expectedDetail = v.detail[basis];
        for (;;) {
          const page = await consumer.call('window_rankings',
            { window_id: v.window_id, kind: v.kind, basis, limit: 50, offset });
          if (!out) {
            out = page;
            assert.equal(out.status, v.state === 'final' ? 'available' : 'partial',
              `${id} ${basis}: status`);
            assert.deepEqual(out.window_state, { state: v.state, reasons: v.state_reasons },
              `${id} ${basis}: window_state`);
            assert.deepEqual(out.coverage, v.counts, `${id} ${basis}: coverage`);
            assert.equal(out.summary.state, v.state, `${id} ${basis}: summary state`);
            if (expectedDetail) {
              assert.ok(page.rows.length > 0, `${id} ${basis}: expected a rank-1 row`);
              assert.equal(page.rows[0].symbol, expectedDetail.symbol, `${id} ${basis}: rank 1`);
              const detail = await consumer.call('result_detail',
                { reference: page.rows[0].detail_reference });
              assert.equal(detail.status, 'available', `${id} ${basis}: detail status`);
              assert.equal(detail.row.symbol, expectedDetail.symbol, `${id} ${basis}: detail row`);
              assert.equal(detail.path.symbol, expectedDetail.symbol, `${id} ${basis}: detail path`);
              for (const f of DETAIL_FIELDS) {
                assert.equal(String(detail.path[`${basis}_${f}`]), String(expectedDetail[f]),
                  `${id} ${basis}: detail ${f}`);
              }
              // The parity trees are publisher-written, so they carry latest.json;
              // the detail path must be digest-pinned to that pointer, not merely
              // consistency-checked against the ranking row.
              assert.equal(detail.paths.verified, 'matches_published_pointer',
                `${id} ${basis}: detail paths pin`);
            } else {
              assert.equal(page.rows.length, 0, `${id} ${basis}: expected no rows`);
            }
          }
          rows.push(...page.rows);
          assert.equal(page.pagination.total, v.rows[basis].length,
            `${id} ${basis}: pagination total`);
          if (!page.pagination.truncated) break;
          offset = page.pagination.next_offset;
          assert.ok(offset > 0, `${id} ${basis}: next_offset`);
        }
        const got = rows.map(r => Object.fromEntries(ROW_FIELDS.map(f => [f, r[f]])));
        assert.deepEqual(got, v.rows[basis], `${id} ${basis}: rows`);
        if (!expectedDetail) {
          assert.equal(rows.length, 0, `${id} ${basis}: expected no rows`);
        }
      }
    }
  });
