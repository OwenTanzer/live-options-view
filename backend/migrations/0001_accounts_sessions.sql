-- Issue #208: accounts and sessions move off Workers KV.
--
-- D1 is SQLite: real transactions, UNIQUE constraints and conditional UPDATEs.
-- That is what replaces KV's read-modify-write retry loop and its missing
-- uniqueness guarantee. Records keep the exact JSON shape they had in KV, so
-- the migration is a copy: password salt/hash/iterations, balance_cash, trades,
-- bot identity and margin fields all move byte-for-byte.
--
-- Legacy KV namespaces are deliberately NOT deleted. They are the rollback
-- source until the cutover has been observed for long enough to retire them,
-- and deleting them is a separate, explicit operation nobody should do while a
-- reverse migration might still be needed.

CREATE TABLE IF NOT EXISTS users (
  username   TEXT PRIMARY KEY,   -- lowercased account identity, as `user:<name>` was
  record     TEXT NOT NULL,      -- the exact legacy JSON record
  version    INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,   -- SHA-256(token) hex; the bearer token is never stored
  username   TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at INTEGER NOT NULL    -- unix seconds
);

CREATE INDEX IF NOT EXISTS sessions_username ON sessions (username);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions (expires_at);

-- Legacy `bot:<name>` markers. The public roster lives in R2 (issue #102) and
-- stays there; these markers exist because old code wrote them and reconciliation
-- must be able to see and retire them.
CREATE TABLE IF NOT EXISTS bot_markers (
  username   TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Restartable migration bookkeeping: cursor per phase plus counts.
CREATE TABLE IF NOT EXISTS migration_state (
  name  TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
