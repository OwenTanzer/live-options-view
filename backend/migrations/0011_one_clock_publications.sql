-- Native immutable publication logs. No account or producer state is modified.
CREATE TABLE IF NOT EXISTS one_clock_publication_work (
  stream TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>0),
  next_sequence INTEGER NOT NULL CHECK(next_sequence>0), last_digest TEXT, last_available_at INTEGER,
  updated_at INTEGER NOT NULL, coverage TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS one_clock_publication_work_insert BEFORE INSERT ON one_clock_publication_work
WHEN NOT EXISTS(SELECT 1 FROM one_clock_publication_work WHERE stream=NEW.stream) AND NEW.generation<>1
BEGIN SELECT RAISE(ABORT,'publication generation conflict'); END;
CREATE TRIGGER IF NOT EXISTS one_clock_publication_work_update BEFORE UPDATE ON one_clock_publication_work
WHEN NEW.generation<>OLD.generation+1 OR NEW.next_sequence<OLD.next_sequence OR NEW.updated_at<OLD.updated_at OR NEW.last_available_at<OLD.last_available_at
BEGIN SELECT RAISE(ABORT,'publication generation conflict'); END;
CREATE TABLE IF NOT EXISTS one_clock_publication_entries (
  stream TEXT NOT NULL, sequence INTEGER NOT NULL, publication_id TEXT NOT NULL,
  digest TEXT NOT NULL, available_at INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(stream,sequence), UNIQUE(stream,publication_id)
);
CREATE TABLE IF NOT EXISTS one_clock_publication_records (
  stream TEXT NOT NULL, source TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
  session TEXT NOT NULL, instrument TEXT NOT NULL, event_at INTEGER NOT NULL,
  available_at INTEGER NOT NULL, received_at INTEGER NOT NULL, sequence INTEGER NOT NULL,
  payload TEXT NOT NULL, PRIMARY KEY(stream,source,record_id,revision)
);
CREATE INDEX IF NOT EXISTS one_clock_publication_record_scope ON one_clock_publication_records(session,instrument,received_at);
CREATE INDEX IF NOT EXISTS one_clock_publication_record_page ON one_clock_publication_records(stream,session,instrument,sequence,source,record_id);
CREATE TRIGGER IF NOT EXISTS one_clock_publication_record_identity BEFORE INSERT ON one_clock_publication_records
WHEN EXISTS(SELECT 1 FROM one_clock_publication_records WHERE stream=NEW.stream AND source=NEW.source AND record_id=NEW.record_id
  AND (session<>NEW.session OR instrument<>NEW.instrument OR revision>=NEW.revision))
BEGIN SELECT RAISE(ABORT,'publication record revision conflict'); END;
CREATE TABLE IF NOT EXISTS one_clock_publication_status (
  stream TEXT NOT NULL, generation INTEGER NOT NULL, checked_at INTEGER NOT NULL,
  coverage TEXT NOT NULL, PRIMARY KEY(stream,generation)
);
