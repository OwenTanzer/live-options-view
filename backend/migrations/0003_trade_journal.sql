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
--   successful sync, the last complete scan and the last read error.

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
  last_summary_json     TEXT
);
