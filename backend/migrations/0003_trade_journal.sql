-- OA-238: read-only mirror of the Trade Retrospectives journal (Notion) for the
-- private trade display. Additive: two new tables, no change to accounts or
-- sessions. The Worker only reads Notion; these rows are its cache and its
-- revision history.
--
-- trade_journal_rows: one row per journal page (= one position).
--   position_json holds the normalised position (executions included); receipt
--   link destinations are never stored. revisions_json is the in-place refresh
--   history. source_status: active | archived | removed | removed_from_source |
--   missing_unverified -- rows that leave the source stay visible, flagged.
-- trade_journal_sync: single row (id = 1) with the sync lease, the last
--   successful sync, the last complete scan, the last read error and the
--   in-progress scan (its start, saved query cursor and whether listing is done).
-- trade_journal_scan: rows listed by the scan in progress (page metadata and
--   whether its body has been processed), so a scan larger than one sync's
--   request budget resumes where it stopped instead of restarting.
-- trade_journal_context: cached market context (underlying + exact-option
--   series read from the existing R2 archives) for positions whose session is
--   over; archived market data does not change, so repeat views cost no reads.

CREATE TABLE IF NOT EXISTS trade_journal_rows (
  page_id            TEXT PRIMARY KEY,
  position_id        TEXT NOT NULL UNIQUE,
  source_status      TEXT NOT NULL DEFAULT 'active',
  supported          INTEGER NOT NULL,
  source_last_edited TEXT NOT NULL,
  body_fetched_at    TEXT NOT NULL,
  content_hash       TEXT NOT NULL,
  revision           INTEGER NOT NULL,
  position_json      TEXT NOT NULL,
  revisions_json     TEXT NOT NULL,
  first_seen_at      TEXT NOT NULL,
  status_changed_at  TEXT
);

CREATE TABLE IF NOT EXISTS trade_journal_sync (
  id                    INTEGER PRIMARY KEY CHECK (id = 1),
  running_since         TEXT,
  last_attempt_at       TEXT,
  last_success_at       TEXT,
  last_complete_scan_at TEXT,
  last_error_json       TEXT,
  last_summary_json     TEXT,
  scan_started_at       TEXT,
  scan_cursor           TEXT,
  scan_query_complete   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trade_journal_scan (
  page_id    TEXT PRIMARY KEY,
  page_json  TEXT NOT NULL,
  processed  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trade_journal_context (
  cache_key     TEXT PRIMARY KEY,
  position_id   TEXT NOT NULL,
  built_at      TEXT NOT NULL,
  context_json  TEXT NOT NULL
);

-- The only principals allowed to enroll are provisioned by the release owner.
CREATE TABLE IF NOT EXISTS trade_journal_members (
  member TEXT PRIMARY KEY CHECK(member IN ('Owen','Jayden','Jacob')),
  invite_hash TEXT UNIQUE, invite_until INTEGER,
  credential_id TEXT UNIQUE, public_key TEXT, counter INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trade_journal_auth_key (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), secret TEXT NOT NULL
);
INSERT OR IGNORE INTO trade_journal_auth_key VALUES (1, hex(randomblob(32)));
CREATE TABLE IF NOT EXISTS trade_journal_sessions (
  token_hash TEXT UNIQUE, credential_id TEXT NOT NULL, challenge TEXT PRIMARY KEY,
  expires INTEGER NOT NULL
);

-- Retain the member name after revocation so routine INSERT OR IGNORE
-- provisioning can never reissue that member's original invitation.
CREATE TRIGGER IF NOT EXISTS trade_journal_revoke_member
BEFORE DELETE ON trade_journal_members
BEGIN
  DELETE FROM trade_journal_sessions WHERE credential_id = OLD.credential_id;
  UPDATE trade_journal_members
     SET invite_hash = NULL, invite_until = NULL, credential_id = NULL,
         public_key = NULL, counter = 0
   WHERE member = OLD.member;
  SELECT RAISE(IGNORE);
END;
