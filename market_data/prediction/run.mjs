// Offline fixture launcher. A licensed runtime can inject an R2 binding and
// normalized read-only events into Collector; there is no venue order endpoint.
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname, sep } from 'node:path';
import { Collector } from './collector.mjs';
const [input, output] = process.argv.slice(2);
if (!input || !output) throw Error('Usage: node market_data/prediction/run.mjs FIXTURE_EVENTS.json NEW_ARCHIVE_DIRECTORY');
const root = resolve(output);
await mkdir(root); // Never overwrite an existing run.
const local = key => {
  if (!/^prediction\/v1\/fixtures\/[a-z0-9/.-]+$/.test(key)) throw Error('Fixture prefix required');
  const path = resolve(root, key);
  if (!path.startsWith(root + sep)) throw Error('Archive boundary');
  return path;
};
const bucket = {
  async put(key, value) { const path = local(key); await mkdir(dirname(path), { recursive: true }); await writeFile(path, value); },
  async get(key) { try { const text = await readFile(local(key), 'utf8'); return { text: async () => text, json: async () => JSON.parse(text) }; } catch (e) { if (e.code === 'ENOENT') return null; throw e; } },
};
const collector = new Collector({ bucket, enabled: true, environment: 'fixtures' });
const events = JSON.parse(await readFile(input, 'utf8'));
for (const event of events) { collector.accept(event); if (collector.pending.length >= 100) await collector.flush(); }
await collector.flush();
console.log(JSON.stringify(collector.status()));
