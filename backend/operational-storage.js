// backend/operational-storage.js -- the sanctioned write path into the R2
// bucket this Worker shares with the capture producer.
//
// WHY this exists: `wrangler.toml` binds `PAPER_TRADES` to
// `qqq-options-chain-data`, the same bucket the collector publishes into.
// Every key under `intraday/`, `raw/`, `prices/`, `derived/`, `baselines/`,
// `macro/`, `settlement/`, `oa203/`, `moo144/`, `squeeze-scanner/` and the
// root `manifest.json` is capture: a Worker write there is not a bug in an
// account feature, it is damage to the published record that the site, the
// MCP server and Crassus all read. The Worker's own state lives in exactly
// two places, so the boundary is an allowlist of *operational* prefixes and
// everything else is refused by default -- a capture prefix introduced later
// is protected without anyone remembering to add it here.
//
// What this is: a code-level boundary over one binding, with every attempted
// write logged. What this is not: an IAM or bucket-policy boundary. Code that
// calls `env.PAPER_TRADES.put(...)` directly still bypasses it, and the
// binding itself still grants the whole bucket -- so a compromised Worker
// revision or a leaked deploy credential can write anywhere. Those bypasses
// are recorded in the boundary report; tests over this module cannot
// establish provider-side permission enforcement.
//
// Reads are deliberately not restricted. The criterion for this boundary is
// about destroying capture, and a read cannot destroy anything; refusing
// reads would only make a future legitimate cross-prefix read fail in a way
// that looks like an outage.

export const OPERATIONAL_PREFIXES = Object.freeze(['system/', 'paper-trades/']);

// R2 rejects a key over 1024 bytes; anything approaching that is a caller bug.
const MAX_KEY_LENGTH = 1024;

export class CaptureWriteRefused extends Error {
  constructor(message, key) {
    super(message);
    this.name = 'CaptureWriteRefused';
    this.key = key;
  }
}

/**
 * Throw unless `key` names an object in the Worker's own operational space.
 *
 * Returns the key so call sites can validate and use in one expression. The
 * checks are deliberately narrow: no absolute paths, no traversal, no empty
 * segments, and a real operational prefix. `Intraday/foo` is a different R2
 * key from `intraday/foo` and is not capture, so case is preserved and
 * compared exactly rather than folded.
 */
export function assertOperationalKey(key) {
  if (typeof key !== 'string' || key.length === 0) {
    throw new CaptureWriteRefused('Operational write key must be a non-empty string', key);
  }
  if (key.length > MAX_KEY_LENGTH) {
    throw new CaptureWriteRefused(`Operational write key exceeds ${MAX_KEY_LENGTH} characters`, key);
  }
  if (key.startsWith('/')) {
    throw new CaptureWriteRefused('Operational write key must be relative to the bucket root', key);
  }
  if (key.includes('\\') || key.includes('\u0000')) {
    throw new CaptureWriteRefused('Operational write key contains an illegal character', key);
  }
  if (key.split('/').some(segment => segment === '' || segment === '.' || segment === '..')) {
    throw new CaptureWriteRefused('Operational write key has an empty, current or parent segment', key);
  }
  if (!OPERATIONAL_PREFIXES.some(prefix => key.startsWith(prefix))) {
    throw new CaptureWriteRefused(
      `Refusing to write capture key "${key}"; this Worker may only write ${OPERATIONAL_PREFIXES.join(' ')}`,
      key,
    );
  }
  return key;
}

/**
 * One JSON log line per attempted mutation. Logging must never change the
 * outcome of the write it is describing, so a failure here is swallowed.
 *
 * Keys only, never values: an execution key carries the caller's own
 * execution id, which the caller already holds, and no body is ever logged.
 */
function recordWrite(record, sink) {
  try {
    sink(JSON.stringify({
      event: 'r2_write',
      binding: 'PAPER_TRADES',
      ...record,
    }));
  } catch {
    // A logging failure is not a storage failure.
  }
}

/**
 * Wrap one request's view of the bucket in the operational boundary.
 *
 * `caller` labels the write path in the log (`bot_index`, `paper_trade`), so
 * a refusal says which feature asked for a capture key. Errors from the
 * binding propagate unchanged -- `updateBotIndex` still inspects `error.status`
 * and `error.code` to tell R2's write throttle from a real failure.
 */
export function operationalBucket(env, caller, sink = console.log) {
  const binding = env?.PAPER_TRADES;
  if (!binding) {
    throw new CaptureWriteRefused('PAPER_TRADES binding is not configured', null);
  }
  const label = typeof caller === 'string' && caller ? caller : 'unspecified';

  return {
    /** Read through. No prefix rule: a read cannot destroy capture. */
    async get(key) {
      return binding.get(key);
    },

    async head(key) {
      return binding.head(key);
    },

    /** Conditional write, unchanged semantics, guarded and logged. */
    async put(key, value, options) {
      try {
        assertOperationalKey(key);
      } catch (error) {
        recordWrite({ outcome: 'refused', caller: label, key: typeof key === 'string' ? key : null,
          reason: error.message }, sink);
        throw error;
      }
      try {
        const stored = await binding.put(key, value, options);
        recordWrite({ outcome: stored ? 'written' : 'not_written', caller: label, key }, sink);
        return stored;
      } catch (error) {
        recordWrite({ outcome: 'failed', caller: label, key,
          error: error?.name ?? 'Error' }, sink);
        throw error;
      }
    },

    async delete(key) {
      try {
        assertOperationalKey(key);
      } catch (error) {
        recordWrite({ outcome: 'refused', caller: label, key: typeof key === 'string' ? key : null,
          reason: error.message }, sink);
        throw error;
      }
      try {
        await binding.delete(key);
        recordWrite({ outcome: 'deleted', caller: label, key }, sink);
      } catch (error) {
        recordWrite({ outcome: 'failed', caller: label, key,
          error: error?.name ?? 'Error' }, sink);
        throw error;
      }
    },
  };
}

/** The raw binding, for the boundary report and for read-only allowlists. */
export function rawOperationalBinding(env) {
  return env?.PAPER_TRADES;
}
