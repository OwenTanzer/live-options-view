// The API Worker owns the one settlement trigger, on named weekdays.
// Cloudflare read numeric "1-5" as Sunday-Thursday; MON-FRI is unambiguous.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const read = f => readFileSync(resolve(ROOT, f), 'utf8');
const cronLines = text => text.split(/\r?\n/).map(l => l.trim()).filter(l => /^crons\s*=/.test(l));
const triggerTables = text => text.split(/\r?\n/).filter(l => l.trim() === '[triggers]').length;

test('API config owns exactly one weekday settlement trigger, MON-FRI', () => {
  const api = read('wrangler.api.toml');
  assert.equal(triggerTables(api), 1);
  const lines = cronLines(api);
  assert.equal(lines.length, 1);
  const crons = JSON.parse(lines[0].replace(/^crons\s*=\s*/, ''));
  assert.deepEqual(crons, ['*/15 12-23 * * MON-FRI']);
  const [minute, hour, dom, month, dow] = crons[0].split(' ');
  assert.equal(minute, '*/15'); assert.equal(hour, '12-23');
  assert.equal(dom, '*'); assert.equal(month, '*');
  assert.doesNotMatch(dow, /\d/, 'day-of-week must use names, never numbers');
});

test('asset config adds no duplicate trigger', () => {
  const assets = read('wrangler.assets.toml');
  assert.equal(triggerTables(assets), 1);
  assert.deepEqual(cronLines(assets), ['crons = []']);
});
