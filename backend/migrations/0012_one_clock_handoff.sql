-- Native cutover receipts retain legacy identities and bounded as-of lookup.
CREATE INDEX IF NOT EXISTS one_clock_publication_status_cutoff ON one_clock_publication_status(stream,checked_at,generation);
CREATE TABLE IF NOT EXISTS one_clock_source_handoffs (
  stream TEXT NOT NULL, session TEXT NOT NULL, instrument TEXT NOT NULL, source TEXT NOT NULL,
  id TEXT NOT NULL UNIQUE, cutover_at INTEGER NOT NULL, native_sequence INTEGER NOT NULL,
  legacy_adapter TEXT NOT NULL, legacy_generation INTEGER NOT NULL,
  PRIMARY KEY(stream,session,instrument,source)
);
CREATE TRIGGER IF NOT EXISTS one_clock_handoff_legacy_generation BEFORE INSERT ON one_clock_source_handoffs
WHEN COALESCE((SELECT generation FROM one_clock_ingestion_work WHERE source=NEW.legacy_adapter AND session=NEW.session AND instrument=NEW.instrument),0)<>NEW.legacy_generation
BEGIN SELECT RAISE(ABORT,'legacy ingestion advanced during handoff'); END;
CREATE TABLE IF NOT EXISTS one_clock_handoff_records (
  handoff_id TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
  PRIMARY KEY(handoff_id,record_id,revision)
);
CREATE TABLE IF NOT EXISTS one_clock_handoff_identities (
  handoff_id TEXT NOT NULL, original_id TEXT NOT NULL, legacy_revision INTEGER NOT NULL,
  native_revision INTEGER NOT NULL, native_sequence INTEGER NOT NULL, baseline_changed INTEGER NOT NULL,
  PRIMARY KEY(handoff_id,original_id)
);
CREATE TRIGGER IF NOT EXISTS one_clock_handoffs_immutable BEFORE UPDATE ON one_clock_source_handoffs
BEGIN SELECT RAISE(ABORT,'immutable source handoff'); END;
CREATE TRIGGER IF NOT EXISTS one_clock_handoff_records_immutable BEFORE UPDATE ON one_clock_handoff_records
BEGIN SELECT RAISE(ABORT,'immutable handoff record'); END;
CREATE TRIGGER IF NOT EXISTS one_clock_handoff_identities_immutable BEFORE UPDATE ON one_clock_handoff_identities
BEGIN SELECT RAISE(ABORT,'immutable handoff identity'); END;
