CREATE TABLE strategy_archive (
  seq INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  version TEXT REFERENCES strategy_archive(id),
  data TEXT NOT NULL CHECK(json_valid(data)),
  submitted_by TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX archive_by_version ON strategy_archive(version, seq);
CREATE UNIQUE INDEX archive_result_identity ON strategy_archive(version, json_extract(data,'$.source'), json_extract(data,'$.resultRef')) WHERE kind='evidence';
CREATE INDEX archive_by_family ON strategy_archive(json_extract(data,'$.family')) WHERE kind='version';
CREATE INDEX archive_by_source ON strategy_archive(json_extract(data,'$.source')) WHERE kind='version';
CREATE INDEX archive_rule_edgeHypothesis ON strategy_archive(json_extract(data,'$.rule.edgeHypothesis') COLLATE NOCASE) WHERE kind='version';
CREATE INDEX archive_rule_entryCondition ON strategy_archive(json_extract(data,'$.rule.entryCondition') COLLATE NOCASE) WHERE kind='version';
CREATE INDEX archive_rule_riskReward ON strategy_archive(json_extract(data,'$.rule.riskReward') COLLATE NOCASE) WHERE kind='version';
CREATE INDEX archive_rule_monitoringCadence ON strategy_archive(json_extract(data,'$.rule.monitoringCadence') COLLATE NOCASE) WHERE kind='version';
CREATE INDEX archive_rule_exitCondition ON strategy_archive(json_extract(data,'$.rule.exitCondition') COLLATE NOCASE) WHERE kind='version';
CREATE TABLE strategy_archive_credit (
  record_id TEXT NOT NULL REFERENCES strategy_archive(id),
  submitter TEXT NOT NULL,
  PRIMARY KEY(record_id, submitter)
);
CREATE INDEX archive_version_parameters ON strategy_archive(json_extract(data,'$.family'),json_extract(data,'$.parameters')) WHERE kind='version';
