-- Additive metadata index. No account, journal, capture object or cron changes.
CREATE TABLE IF NOT EXISTS one_clock_catalog_state (
  source TEXT PRIMARY KEY, source_version TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>0), cursor TEXT,
  cycle_started_at INTEGER NOT NULL, complete_through INTEGER,
  updated_at INTEGER NOT NULL, failures INTEGER NOT NULL DEFAULT 0
);
CREATE TRIGGER IF NOT EXISTS one_clock_catalog_insert BEFORE INSERT ON one_clock_catalog_state
WHEN NOT EXISTS(SELECT 1 FROM one_clock_catalog_state WHERE source=NEW.source) AND NEW.generation<>1
BEGIN SELECT RAISE(ABORT, 'catalog generation conflict'); END;
CREATE TRIGGER IF NOT EXISTS one_clock_catalog_update BEFORE UPDATE ON one_clock_catalog_state
WHEN NEW.generation<>OLD.generation+1 OR NEW.source_version<>OLD.source_version
BEGIN SELECT RAISE(ABORT, 'catalog generation conflict'); END;
CREATE TABLE IF NOT EXISTS one_clock_catalog_entries (
  source TEXT NOT NULL, object_key TEXT NOT NULL, record_group TEXT NOT NULL,
  record_kind TEXT NOT NULL, uploaded_at INTEGER NOT NULL,
  size INTEGER NOT NULL, etag TEXT NOT NULL,
  PRIMARY KEY(source,object_key)
);
CREATE INDEX IF NOT EXISTS one_clock_catalog_time ON one_clock_catalog_entries(source,uploaded_at,record_group);
CREATE INDEX IF NOT EXISTS one_clock_catalog_group ON one_clock_catalog_entries(source,record_group,record_kind,uploaded_at);
-- Dyadic interval decomposition supports bounded point queries without scanning
-- completed history or expanding long gaps into one record per calendar day.
CREATE TABLE IF NOT EXISTS one_clock_catalog_gaps (
  source TEXT NOT NULL, bucket TEXT NOT NULL, record_group TEXT NOT NULL,
  PRIMARY KEY(source,bucket,record_group)
);
CREATE INDEX IF NOT EXISTS one_clock_catalog_gap_group ON one_clock_catalog_gaps(source,record_group);
