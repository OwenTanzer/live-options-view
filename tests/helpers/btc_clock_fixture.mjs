import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Exercise the collector wire decoder, not a second JavaScript fixture builder.
let cached;
export function btcClockScenarios() {
  cached ??= JSON.parse(execFileSync(process.env.PYTHON || 'python',
    [fileURLToPath(new URL('./btc_clock_fixture.py', import.meta.url))], { encoding: 'utf8' }));
  return structuredClone(cached);
}
