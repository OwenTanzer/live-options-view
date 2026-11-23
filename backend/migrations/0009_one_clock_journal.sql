-- Private revision payloads, observed when committed to this application's D1.
-- No source-edit/fill timestamp is promoted to proof of prior availability.
CREATE TABLE IF NOT EXISTS trade_journal_history (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  page_id TEXT NOT NULL, position_id TEXT NOT NULL,
  source_status TEXT NOT NULL, revision INTEGER NOT NULL,
  content_hash TEXT NOT NULL, position_json TEXT NOT NULL,
  recorded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS trade_journal_history_position ON trade_journal_history(position_id,recorded_at,snapshot_id);
CREATE INDEX IF NOT EXISTS trade_journal_history_time ON trade_journal_history(recorded_at,snapshot_id);
-- Existing payloads become known at migration, never at a backdated fill time.
INSERT INTO trade_journal_history(page_id,position_id,source_status,revision,content_hash,position_json,recorded_at)
SELECT page_id,position_id,source_status,revision,content_hash,position_json,
  CAST(ROUND((julianday('now')-2440587.5)*86400000) AS INTEGER)
FROM trade_journal_rows r WHERE NOT EXISTS(SELECT 1 FROM trade_journal_history h WHERE h.page_id=r.page_id);
CREATE TRIGGER IF NOT EXISTS trade_journal_history_insert AFTER INSERT ON trade_journal_rows
BEGIN
  INSERT INTO trade_journal_history(page_id,position_id,source_status,revision,content_hash,position_json,recorded_at)
  VALUES(NEW.page_id,NEW.position_id,NEW.source_status,NEW.revision,NEW.content_hash,NEW.position_json,
    CAST(ROUND((julianday('now')-2440587.5)*86400000) AS INTEGER));
END;
CREATE TRIGGER IF NOT EXISTS trade_journal_history_update AFTER UPDATE ON trade_journal_rows
WHEN NEW.content_hash<>OLD.content_hash OR NEW.source_status<>OLD.source_status OR NEW.position_id<>OLD.position_id
BEGIN
  INSERT INTO trade_journal_history(page_id,position_id,source_status,revision,content_hash,position_json,recorded_at)
  VALUES(NEW.page_id,NEW.position_id,NEW.source_status,NEW.revision,NEW.content_hash,NEW.position_json,
    CAST(ROUND((julianday('now')-2440587.5)*86400000) AS INTEGER));
END;
CREATE TRIGGER IF NOT EXISTS trade_journal_history_delete AFTER DELETE ON trade_journal_rows
BEGIN
  INSERT INTO trade_journal_history(page_id,position_id,source_status,revision,content_hash,position_json,recorded_at)
  VALUES(OLD.page_id,OLD.position_id,'removed',OLD.revision,OLD.content_hash,OLD.position_json,
    CAST(ROUND((julianday('now')-2440587.5)*86400000) AS INTEGER));
END;
