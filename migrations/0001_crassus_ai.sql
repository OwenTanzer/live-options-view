-- Crassus AI durable storage: versioned parameter overrides + a decision-
-- ledger durability mirror. See crassus/crassus/policy.py for the trust
-- boundary these tables exist to support -- this is storage only, not a
-- source of authority: whether an `accepted` row here ever changes a
-- strategy's behavior is decided independently, every cycle, by the Python
-- policy layer.
--
-- Apply with: wrangler d1 execute crassus_ai --file=migrations/0001_crassus_ai.sql

CREATE TABLE IF NOT EXISTS crassus_overrides (
  id TEXT PRIMARY KEY,
  account_alias TEXT NOT NULL,
  status TEXT NOT NULL,              -- proposed | accepted | rejected | expired | superseded
  previous_params TEXT NOT NULL,     -- JSON: full baseline params at proposal time
  proposed_params TEXT NOT NULL,     -- JSON: only the changed keys
  rationale TEXT NOT NULL,
  evidence_refs TEXT NOT NULL,       -- JSON array of decision_ids / source refs
  model TEXT NOT NULL,               -- model/version string, e.g. "claude-sonnet-5"
  created_utc TEXT NOT NULL,
  expires_utc TEXT NOT NULL,
  accepted_utc TEXT,
  accepted_by TEXT,                  -- operator identity, set only via /accept
  rollback_target TEXT,              -- id of the override to revert to
  schema_version TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_overrides_account_status
  ON crassus_overrides(account_alias, status, created_utc);

CREATE TABLE IF NOT EXISTS crassus_ledger_mirror (
  decision_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  account_alias TEXT NOT NULL,
  outcome_class TEXT NOT NULL,
  timestamp_utc TEXT NOT NULL,
  record TEXT NOT NULL               -- full JSON of the MOO-24 audit record
);

CREATE INDEX IF NOT EXISTS idx_ledger_account_time
  ON crassus_ledger_mirror(account_alias, timestamp_utc);
