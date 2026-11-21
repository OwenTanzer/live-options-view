-- Shared public evidence ingestion. Private sources never enter these tables.
CREATE TABLE IF NOT EXISTS one_clock_ingestion_work (
  source TEXT NOT NULL, session TEXT NOT NULL, instrument TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>0), updated_at INTEGER NOT NULL,
  coverage TEXT NOT NULL, PRIMARY KEY(source,session,instrument)
);
CREATE TRIGGER IF NOT EXISTS one_clock_ingestion_insert BEFORE INSERT ON one_clock_ingestion_work
WHEN NOT EXISTS(SELECT 1 FROM one_clock_ingestion_work WHERE source=NEW.source AND session=NEW.session AND instrument=NEW.instrument) AND NEW.generation<>1
BEGIN SELECT RAISE(ABORT,'ingestion generation conflict'); END;
CREATE TRIGGER IF NOT EXISTS one_clock_ingestion_update BEFORE UPDATE ON one_clock_ingestion_work
WHEN NEW.generation<>OLD.generation+1 OR NEW.updated_at<OLD.updated_at
BEGIN SELECT RAISE(ABORT,'ingestion generation conflict'); END;
CREATE TABLE IF NOT EXISTS one_clock_ingestion_versions (
  source TEXT NOT NULL, session TEXT NOT NULL, instrument TEXT NOT NULL,
  artifact TEXT NOT NULL, version TEXT NOT NULL, received_at INTEGER NOT NULL,
  status TEXT NOT NULL, detail TEXT NOT NULL,
  PRIMARY KEY(source,session,instrument,artifact,version)
);
CREATE TABLE IF NOT EXISTS one_clock_ingested (
  source TEXT NOT NULL, session TEXT NOT NULL, instrument TEXT NOT NULL, origin TEXT NOT NULL,
  record_id TEXT NOT NULL, revision INTEGER NOT NULL, event_at INTEGER NOT NULL,
  available_at INTEGER NOT NULL, received_at INTEGER NOT NULL, collection TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(source,session,instrument,origin,record_id,revision)
);
CREATE INDEX IF NOT EXISTS one_clock_ingested_scope ON one_clock_ingested(session,instrument,origin,received_at);
CREATE TRIGGER IF NOT EXISTS one_clock_ingested_immutable BEFORE INSERT ON one_clock_ingested
WHEN EXISTS(SELECT 1 FROM one_clock_ingested WHERE source=NEW.source AND session=NEW.session AND instrument=NEW.instrument AND origin=NEW.origin AND record_id=NEW.record_id AND revision=NEW.revision AND payload<>NEW.payload)
BEGIN SELECT RAISE(ABORT,'immutable ingested observation conflict'); END;
CREATE TABLE IF NOT EXISTS one_clock_ingestion_history (
  source TEXT NOT NULL, session TEXT NOT NULL, instrument TEXT NOT NULL,
  generation INTEGER NOT NULL, recorded_at INTEGER NOT NULL, coverage TEXT NOT NULL,
  PRIMARY KEY(source,session,instrument,generation)
);
CREATE INDEX IF NOT EXISTS one_clock_ingestion_history_cutoff ON one_clock_ingestion_history(session,instrument,recorded_at);
CREATE TABLE IF NOT EXISTS one_clock_activations (
  namespace TEXT NOT NULL, rule_id TEXT NOT NULL, version INTEGER NOT NULL,
  started_at INTEGER NOT NULL, ended_at INTEGER,
  PRIMARY KEY(namespace,rule_id,version,started_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_clock_one_active_version ON one_clock_activations(namespace,rule_id) WHERE ended_at IS NULL;
CREATE TRIGGER IF NOT EXISTS one_clock_active_capacity BEFORE INSERT ON one_clock_activations
WHEN NEW.ended_at IS NULL AND ((SELECT COUNT(*) FROM one_clock_activations WHERE ended_at IS NULL)>=1000 OR
  (NOT EXISTS(SELECT 1 FROM one_clock_activations WHERE namespace=NEW.namespace AND ended_at IS NULL) AND
   (SELECT COUNT(DISTINCT namespace) FROM one_clock_activations WHERE ended_at IS NULL)>=100))
BEGIN SELECT RAISE(ABORT,'observer active capacity bound'); END;
CREATE TRIGGER IF NOT EXISTS one_clock_registered_limit BEFORE INSERT ON one_clock_rules
WHEN NOT EXISTS(SELECT 1 FROM one_clock_rules WHERE namespace=NEW.namespace AND rule_id=NEW.rule_id AND version=NEW.version)
  AND (SELECT COUNT(*) FROM one_clock_rules WHERE namespace=NEW.namespace)>=100
BEGIN SELECT RAISE(ABORT,'rule version bound'); END;
CREATE TABLE IF NOT EXISTS one_clock_evaluation_work (
  namespace TEXT NOT NULL, session TEXT NOT NULL, instrument TEXT NOT NULL,
  rule_cursor INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(namespace,session,instrument)
);
CREATE INDEX IF NOT EXISTS one_clock_evaluation_due ON one_clock_evaluation_work(session,updated_at);
