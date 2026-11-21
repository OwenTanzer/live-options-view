-- Current ordinary paper session + independently verified existing GitHub owner.
-- No OAuth credentials, user provisioning, raw cookies, or username grants.
CREATE TABLE IF NOT EXISTS btc_github_sessions (
  token_hash TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  github_id INTEGER NOT NULL CHECK(github_id=125096971),
  source_binding TEXT NOT NULL,
  expires INTEGER NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1))
);
CREATE TRIGGER IF NOT EXISTS btc_github_logout AFTER DELETE ON sessions BEGIN
  DELETE FROM btc_github_sessions WHERE token_hash=OLD.token_hash;
END;
CREATE TRIGGER IF NOT EXISTS btc_github_account_deleted AFTER DELETE ON users BEGIN
  DELETE FROM btc_github_sessions WHERE username=OLD.username;
END;
