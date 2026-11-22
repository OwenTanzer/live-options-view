-- Bound market-availability replay separately from observer receipt queries.
CREATE INDEX IF NOT EXISTS one_clock_native_chain_replay
ON one_clock_publication_records (stream, session, source, event_at DESC, available_at DESC, record_id DESC, revision DESC)
WHERE source='price';
