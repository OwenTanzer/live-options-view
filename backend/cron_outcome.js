// backend/cron_outcome.js (E2) -- structured outcome records for the
// settlement Cron Trigger.
//
// Each scheduled invocation emits, as one-line JSON via console.log /
// console.error (Workers Logs / Tail pick these up):
//   phase "invoked"    -- the handler started (always first)
//   phase "completed"  -- the run finished: outcome "settled" or "no_op",
//                         with a no_op_reason and counts-only totals
//   phase "error"      -- the run threw: an allowlisted error_category with a
//                         fixed message (never the error's own text); the
//                         original error is re-thrown afterwards so the
//                         invocation still fails exactly as it did before
// All three share run_id, cron and scheduled_time, and carry an ISO
// timestamp, so a retained invocation can be matched to its outcome.
//
// Privacy: records hold counts, the as-of date and, on error, only values from
// the fixed tables below. An error's own name and message are never logged:
// e.g. a JSON.parse failure on a stored account record quotes part of that
// record, so truncation is not a privacy boundary. Known errors thrown by the
// sweep's own code are matched by their exact constant message; anything
// else is "unclassified" with a generic message. Records never include
// usernames, balances, trades, prices, tokens or request bodies; the summary
// is whitelisted field by field.

export const CRON_EVENT = 'settlement_cron';

const SUMMARY_FIELDS = [
  'as_of', 'accounts_indexed', 'accounts_ineligible', 'accounts_with_expired',
  'expirations_needed', 'marks_resolved', 'marks_missing', 'mark_errors',
  'accounts_settled', 'positions_settled', 'accounts_unchanged',
  'accounts_liquidated', 'accounts_retry',
];

// Exact messages thrown on the sweep path (backend/bots.js botUsernames /
// loadBotIndex, backend/settlement.js settleAllBots / liquidateAccount)
// -> category. Matching is by equality against these constants only.
const KNOWN_ERRORS = new Map([
  ['Bot membership migration is not ready', 'bot_index_not_ready'],
  ['Invalid bot index mode', 'bot_index_mode_invalid'],
  ['Invalid bot membership index', 'bot_index_invalid'],
  ['Invalid bot membership', 'bot_index_invalid'],
  ['Indexed settlement account unavailable', 'settlement_account_unavailable'],
  ['Bot closure could not be persisted', 'liquidation_not_persisted'],
]);
export const ERROR_MESSAGES = Object.freeze({
  bot_index_not_ready: 'bot membership index is not ready',
  bot_index_mode_invalid: 'bot index read mode is invalid',
  bot_index_invalid: 'bot membership index is invalid',
  settlement_account_unavailable: 'an indexed settlement account could not be read',
  liquidation_not_persisted: 'an insolvent account closure could not be persisted',
  unclassified: 'unclassified error; details withheld',
});
const SAFE_ERROR_CLASSES = new Set(['Error', 'TypeError', 'SyntaxError', 'RangeError',
  'ReferenceError', 'AggregateError', 'EvalError', 'URIError']);

export function classifyError(error) {
  const message = typeof error?.message === 'string' ? error.message : null;
  const category = (message !== null && KNOWN_ERRORS.get(message)) || 'unclassified';
  const name = error?.constructor?.name;
  return {
    error_category: category,
    error_class: SAFE_ERROR_CLASSES.has(name) && error?.name === name ? name : 'other',
    error_message: ERROR_MESSAGES[category],
  };
}

function isoOrNull(ms) {
  return typeof ms === 'number' && Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}

function newRunId() {
  return globalThis.crypto?.randomUUID?.() ?? `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function createCronOutcomeLog(event, { now = () => Date.now(), log = console.log,
  logError = console.error, runId = newRunId() } = {}) {
  const base = {
    event: CRON_EVENT,
    run_id: runId,
    cron: typeof event?.cron === 'string' ? event.cron : null,
    scheduled_time: isoOrNull(event?.scheduledTime),
  };
  const startedAt = now();
  let finished = false;
  const emit = (sink, record) => sink(JSON.stringify({ ...base, ...record }));

  return {
    invoked() {
      emit(log, { phase: 'invoked', at: new Date(startedAt).toISOString() });
    },
    completed(summary) {
      if (finished) return;
      finished = true;
      const at = now();
      const counts = {};
      for (const field of SUMMARY_FIELDS) {
        if (summary && Object.hasOwn(summary, field)) counts[field] = summary[field];
      }
      const settledSomething = (summary?.accounts_settled ?? 0) > 0 || (summary?.accounts_liquidated ?? 0) > 0;
      emit(log, {
        phase: 'completed',
        outcome: settledSomething ? 'settled' : 'no_op',
        no_op_reason: settledSomething ? null : (summary?.no_op_reason ?? 'unspecified'),
        at: new Date(at).toISOString(),
        duration_ms: at - startedAt,
        ...counts,
      });
    },
    error(stage, error) {
      if (finished) return;
      finished = true;
      const at = now();
      emit(logError, {
        phase: 'error',
        stage,
        ...classifyError(error),
        at: new Date(at).toISOString(),
        duration_ms: at - startedAt,
      });
    },
  };
}
