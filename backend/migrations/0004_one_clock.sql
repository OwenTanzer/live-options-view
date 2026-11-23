-- Additive observer storage. No existing account, session or capture table changes.
CREATE TABLE IF NOT EXISTS one_clock_rules (
  namespace TEXT NOT NULL, rule_id TEXT NOT NULL, version INTEGER NOT NULL,
  registered_at INTEGER NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(namespace, rule_id, version)
);
CREATE TRIGGER IF NOT EXISTS one_clock_rule_immutable BEFORE INSERT ON one_clock_rules
WHEN EXISTS(SELECT 1 FROM one_clock_rules WHERE namespace=NEW.namespace AND rule_id=NEW.rule_id AND version=NEW.version AND payload<>NEW.payload)
BEGIN SELECT RAISE(ABORT, 'immutable rule version conflict'); END;

CREATE TABLE IF NOT EXISTS one_clock_state (
  namespace TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>0), cutoff INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS one_clock_state_insert BEFORE INSERT ON one_clock_state
WHEN NOT EXISTS(SELECT 1 FROM one_clock_state WHERE namespace=NEW.namespace) AND NEW.generation<>1
BEGIN SELECT RAISE(ABORT, 'observer generation conflict'); END;
CREATE TRIGGER IF NOT EXISTS one_clock_state_update BEFORE UPDATE ON one_clock_state
WHEN NEW.generation<>OLD.generation+1 OR NEW.cutoff<OLD.cutoff
BEGIN SELECT RAISE(ABORT, 'observer generation conflict'); END;

CREATE TABLE IF NOT EXISTS one_clock_observations (
  namespace TEXT NOT NULL, source TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
  session TEXT NOT NULL, instrument TEXT NOT NULL, origin TEXT NOT NULL,
  received_at INTEGER NOT NULL, collection TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(namespace, source, record_id, revision, session, instrument, origin)
);
CREATE INDEX IF NOT EXISTS one_clock_observation_scope ON one_clock_observations(namespace,session,instrument,origin);
CREATE TRIGGER IF NOT EXISTS one_clock_observation_immutable BEFORE INSERT ON one_clock_observations
WHEN EXISTS(SELECT 1 FROM one_clock_observations WHERE namespace=NEW.namespace AND source=NEW.source AND record_id=NEW.record_id AND revision=NEW.revision AND session=NEW.session AND instrument=NEW.instrument AND origin=NEW.origin AND payload<>NEW.payload)
BEGIN SELECT RAISE(ABORT, 'immutable observation revision conflict'); END;

CREATE TABLE IF NOT EXISTS one_clock_checkpoints (
  namespace TEXT NOT NULL, source TEXT NOT NULL, cursor TEXT NOT NULL, updated_at INTEGER NOT NULL,
  PRIMARY KEY(namespace,source)
);
CREATE TABLE IF NOT EXISTS one_clock_receipts (
  namespace TEXT NOT NULL, batch_id TEXT NOT NULL, digest TEXT NOT NULL, generation INTEGER NOT NULL,
  committed_at INTEGER NOT NULL, PRIMARY KEY(namespace,batch_id)
);

CREATE TABLE IF NOT EXISTS one_clock_events (
  namespace TEXT NOT NULL, event_id TEXT NOT NULL, session TEXT NOT NULL, instrument TEXT NOT NULL,
  origin TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
  PRIMARY KEY(namespace,event_id)
);
CREATE INDEX IF NOT EXISTS one_clock_event_scope ON one_clock_events(namespace,session,instrument,origin);
CREATE TABLE IF NOT EXISTS one_clock_event_history (
  namespace TEXT NOT NULL, event_id TEXT NOT NULL, revision INTEGER NOT NULL,
  recorded_at INTEGER NOT NULL, batch_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(namespace,event_id,revision)
);
CREATE INDEX IF NOT EXISTS one_clock_history_cutoff ON one_clock_event_history(namespace,recorded_at);
