-- Append-only, per-scope invalidations. An entry may correct several sessions.
CREATE TABLE IF NOT EXISTS one_clock_native_changes (
  generation INTEGER PRIMARY KEY,
  stream TEXT NOT NULL, sequence INTEGER NOT NULL,
  session TEXT NOT NULL, instrument TEXT NOT NULL, received_at INTEGER NOT NULL, bootstrap INTEGER NOT NULL DEFAULT 0,
  UNIQUE(stream,sequence,session)
);
CREATE INDEX IF NOT EXISTS one_clock_changes_at_cutoff ON one_clock_native_changes
  (session,instrument,received_at,generation);
CREATE INDEX IF NOT EXISTS one_clock_changes_at_generation ON one_clock_native_changes
  (session,instrument,generation);
CREATE TABLE IF NOT EXISTS one_clock_change_lanes (
  session TEXT NOT NULL,instrument TEXT NOT NULL,stream TEXT NOT NULL,bootstrap INTEGER NOT NULL,
  PRIMARY KEY(session,instrument,stream,bootstrap)
);
CREATE INDEX IF NOT EXISTS one_clock_lane_at_time ON one_clock_native_changes(session,instrument,stream,bootstrap,received_at,generation);
CREATE INDEX IF NOT EXISTS one_clock_lane_at_generation ON one_clock_native_changes(session,instrument,stream,bootstrap,generation);
CREATE TRIGGER IF NOT EXISTS one_clock_publication_changed AFTER INSERT ON one_clock_publication_entries
BEGIN
  INSERT INTO one_clock_native_changes(stream,sequence,session,instrument,received_at)
  SELECT DISTINCT NEW.stream,NEW.sequence,json_extract(value,'$.session'),
    substr(NEW.stream,instr(NEW.stream,'/')+1),NEW.received_at
    FROM json_each(NEW.payload,'$.records');
END;
CREATE TABLE IF NOT EXISTS one_clock_change_bootstrap (
  stream TEXT PRIMARY KEY, last_sequence INTEGER NOT NULL, end_sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS one_clock_generation_receipts (
  namespace TEXT NOT NULL, session TEXT NOT NULL, instrument TEXT NOT NULL,
  rule_id TEXT NOT NULL, version INTEGER NOT NULL, generation INTEGER NOT NULL,
  committed_at INTEGER NOT NULL, frozen INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(namespace,session,instrument,rule_id,version,generation)
);
CREATE INDEX IF NOT EXISTS one_clock_completed_at_cutoff ON one_clock_generation_receipts(namespace,session,instrument,rule_id,version,committed_at,generation);
CREATE TRIGGER IF NOT EXISTS one_clock_generation_monotonic BEFORE INSERT ON one_clock_generation_receipts
WHEN NEW.generation + (1-NEW.frozen) <= COALESCE((SELECT MAX(generation) FROM one_clock_generation_receipts WHERE
  namespace=NEW.namespace AND session=NEW.session AND instrument=NEW.instrument AND
  rule_id=NEW.rule_id AND version=NEW.version),-1)
BEGIN SELECT RAISE(ABORT,'stale historical evaluation'); END;
CREATE TABLE IF NOT EXISTS one_clock_historical_rules (
  namespace TEXT NOT NULL, session TEXT NOT NULL, instrument TEXT NOT NULL,
  rule_id TEXT NOT NULL, version INTEGER NOT NULL,
  target INTEGER NOT NULL, next_target INTEGER NOT NULL, enqueued_at INTEGER NOT NULL,
  retry_at INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, reason TEXT,
  PRIMARY KEY(namespace,session,instrument,rule_id,version)
);
CREATE INDEX IF NOT EXISTS one_clock_historical_due ON one_clock_historical_rules(retry_at,enqueued_at);
CREATE TABLE IF NOT EXISTS one_clock_interval_index (
  instrument TEXT NOT NULL, bucket INTEGER NOT NULL, namespace TEXT NOT NULL,
  rule_id TEXT NOT NULL, version INTEGER NOT NULL, started_at INTEGER NOT NULL,
  PRIMARY KEY(instrument,bucket,namespace,rule_id,version,started_at)
);
CREATE INDEX IF NOT EXISTS one_clock_interval_identity ON one_clock_interval_index(namespace,rule_id,version,started_at);
CREATE TABLE IF NOT EXISTS one_clock_interval_build (
  namespace TEXT NOT NULL, rule_id TEXT NOT NULL, version INTEGER NOT NULL, started_at INTEGER NOT NULL, ended_at INTEGER
);
-- A binary interval tree covers UTC epoch days 0..131071. A closed/open
-- interval takes at most 34 complete nodes; lookup probes only the ancestors
-- of the requested trading day and confirms the exact market-time overlap.
CREATE TRIGGER IF NOT EXISTS one_clock_index_interval AFTER INSERT ON one_clock_interval_build
BEGIN
  DELETE FROM one_clock_interval_index WHERE namespace=NEW.namespace AND rule_id=NEW.rule_id AND version=NEW.version AND started_at=NEW.started_at;
  INSERT INTO one_clock_interval_index(instrument,bucket,namespace,rule_id,version,started_at)
    WITH RECURSIVE days(a,b) AS (SELECT NEW.started_at/86400000,COALESCE(NEW.ended_at/86400000,131071)),
    nodes(bucket,lo,hi) AS (
      SELECT 1,0,131071 UNION ALL
      SELECT bucket*2+j.value,CASE WHEN j.value=0 THEN lo ELSE (lo+hi)/2+1 END,
        CASE WHEN j.value=0 THEN (lo+hi)/2 ELSE hi END
      FROM nodes,days,json_each('[0,1]') j WHERE lo<hi AND hi>=a AND lo<=b AND (lo<a OR hi>b)
    )
    SELECT DISTINCT i.value,bucket,NEW.namespace,NEW.rule_id,NEW.version,NEW.started_at
    FROM nodes,days,one_clock_rules r,json_each(r.payload,'$.instruments') i
    WHERE lo>=a AND hi<=b AND r.namespace=NEW.namespace AND r.rule_id=NEW.rule_id AND r.version=NEW.version;
  DELETE FROM one_clock_interval_build WHERE rowid=NEW.rowid;
END;
CREATE TRIGGER IF NOT EXISTS one_clock_activation_index_0 AFTER INSERT ON one_clock_activations
BEGIN
  INSERT INTO one_clock_interval_build VALUES(NEW.namespace,NEW.rule_id,NEW.version,NEW.started_at,NEW.ended_at);
END;
CREATE TRIGGER IF NOT EXISTS one_clock_activation_index_1 AFTER UPDATE OF ended_at ON one_clock_activations
BEGIN
  INSERT INTO one_clock_interval_build VALUES(NEW.namespace,NEW.rule_id,NEW.version,NEW.started_at,NEW.ended_at);
END;
CREATE TABLE IF NOT EXISTS one_clock_interval_bootstrap (id INTEGER PRIMARY KEY CHECK(id=1),cursor TEXT NOT NULL,done INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS one_clock_fanout (
  session TEXT NOT NULL,instrument TEXT NOT NULL,target INTEGER NOT NULL,next_target INTEGER NOT NULL,
  enqueued_at INTEGER NOT NULL,cursor TEXT NOT NULL DEFAULT '[]',retry_at INTEGER NOT NULL DEFAULT 0,reason TEXT,PRIMARY KEY(session,instrument)
);
CREATE INDEX IF NOT EXISTS one_clock_fanout_due ON one_clock_fanout(retry_at,enqueued_at);
CREATE TRIGGER IF NOT EXISTS one_clock_enqueue_change AFTER INSERT ON one_clock_native_changes
BEGIN
  INSERT OR IGNORE INTO one_clock_change_lanes VALUES(NEW.session,NEW.instrument,NEW.stream,NEW.bootstrap);
  INSERT INTO one_clock_fanout(session,instrument,target,next_target,enqueued_at)
    VALUES(NEW.session,NEW.instrument,NEW.generation,NEW.generation,NEW.received_at)
    ON CONFLICT(session,instrument) DO UPDATE SET next_target=NEW.generation;
END;
INSERT OR IGNORE INTO one_clock_change_bootstrap(stream,last_sequence,end_sequence) SELECT stream,0,next_sequence-1 FROM one_clock_publication_work WHERE next_sequence>1;
