-- Deliberate proof for one existing paper session. No usernames are provisioned
-- here and no journal login cookie, invitation, or secret is reused.
CREATE TABLE IF NOT EXISTS btc_owner_challenges (
  token_hash TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  challenge TEXT NOT NULL UNIQUE,
  source_binding TEXT NOT NULL,
  expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS btc_owner_sessions (
  token_hash TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  credential_id TEXT NOT NULL,
  source_binding TEXT NOT NULL,
  expires INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS btc_owner_credential ON btc_owner_sessions(credential_id);
CREATE TRIGGER IF NOT EXISTS btc_owner_logout AFTER DELETE ON sessions BEGIN
  DELETE FROM btc_owner_challenges WHERE token_hash=OLD.token_hash;
  DELETE FROM btc_owner_sessions WHERE token_hash=OLD.token_hash;
END;
CREATE TRIGGER IF NOT EXISTS btc_owner_account_deleted AFTER DELETE ON users BEGIN
  DELETE FROM btc_owner_challenges WHERE username=OLD.username;
  DELETE FROM btc_owner_sessions WHERE username=OLD.username;
END;
-- The existing journal revocation trigger clears the credential on UPDATE.
CREATE TRIGGER IF NOT EXISTS btc_owner_credential_revoked
AFTER UPDATE OF credential_id, public_key ON trade_journal_members
WHEN OLD.credential_id IS NOT NEW.credential_id OR OLD.public_key IS NOT NEW.public_key
BEGIN
  DELETE FROM btc_owner_sessions WHERE credential_id=OLD.credential_id;
END;
