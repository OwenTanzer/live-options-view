-- Issue #208, cutover support.
--
-- The session copy hashes each KV session key, so D1 cannot recover the raw
-- token from a row. To make revocation converge -- a session logged out (or
-- revoked by settlement) between two copy passes must not be resurrected by the
-- next pass -- the copy records the hashes the full scan actually saw. Under the
-- armed fence a full scan is authoritative, so any D1 session whose hash is
-- absent from this table is deleted by the reconcile phase.

CREATE TABLE IF NOT EXISTS migration_session_seen (
  token_hash TEXT PRIMARY KEY,
  seen_at    TEXT NOT NULL
);
