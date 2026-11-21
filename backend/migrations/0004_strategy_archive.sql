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
CREATE TABLE strategy_archive_credit (
  record_id TEXT NOT NULL REFERENCES strategy_archive(id),
  submitter TEXT NOT NULL,
  PRIMARY KEY(record_id, submitter)
);
INSERT INTO strategy_archive(id,kind,version,data,submitted_by) VALUES ('spy-200','version',NULL,'{"family":"Moving-average crossover","horizon":"Daily","instrument":"SPY shares","originator":"Crassus","parameters":{"exposure":{"type":"number","unit":"% of equity","value":200},"lookback":{"type":"integer","unit":"sessions","value":200}},"parent":null,"rules":"Bullish cross targets 200% long. Bearish cross targets cash. Hold between crosses.","signal":"Completed daily adjusted close crosses its simple moving average","source":"https://github.com/OwenTanzer/live-options-view/issues/97","stage":"documented"}','Musef');
INSERT INTO strategy_archive_credit VALUES ('spy-200','Musef');
INSERT INTO strategy_archive(id,kind,version,data,submitted_by) VALUES ('spy-200-nov10','evidence','spy-200','{"assumptions":"Adjusted close; no crossover at observation.","kind":"paper","period":"2026-11-10","sample":"One checkpoint","resultRef":"nov10-checkpoint","scope":"local","source":"https://github.com/OwenTanzer/live-options-view/issues/97","summary":"Paper runner processed a completed daily bar. Zero trades observed at this checkpoint."}','Musef');
INSERT INTO strategy_archive_credit VALUES ('spy-200-nov10','Musef');
