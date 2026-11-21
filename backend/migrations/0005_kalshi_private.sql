-- Owner's personal research. No R2/public archive or Workers KV binding.
CREATE TABLE IF NOT EXISTS kalshi_state (
  account_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (account_id, key)
);
CREATE TABLE IF NOT EXISTS kalshi_budget (
  account_id TEXT NOT NULL, scope TEXT NOT NULL, slot INTEGER NOT NULL, used INTEGER NOT NULL,
  PRIMARY KEY (account_id, scope)
);
CREATE TABLE IF NOT EXISTS kalshi_evidence (
  account_id TEXT NOT NULL, id TEXT NOT NULL, received_at TEXT NOT NULL,
  market TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY (account_id, id)
);
CREATE INDEX IF NOT EXISTS kalshi_evidence_time ON kalshi_evidence(account_id, received_at);
-- Retention is explicit and bounded; fills retain their original evidence inline.
