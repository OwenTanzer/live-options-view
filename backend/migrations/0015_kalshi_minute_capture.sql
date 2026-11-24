-- Two alternating slots allow the previous minute to finish its archive write.
CREATE TABLE IF NOT EXISTS kalshi_capture_lease (
  slot INTEGER PRIMARY KEY CHECK (slot IN (0, 1)),
  minute INTEGER NOT NULL,
  token TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS kalshi_minute_catalog_ref ON kalshi_evidence (
  account_id, json_extract(payload, '$.catalog_receipt'), received_at
) WHERE market = 'Minute chain';
