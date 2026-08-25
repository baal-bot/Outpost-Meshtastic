CREATE TABLE web_credential (
  id INTEGER PRIMARY KEY CHECK (id=1),
  password_hash TEXT NOT NULL,
  must_change INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  changed_at INTEGER
);

CREATE TABLE web_session (
  token_hash TEXT PRIMARY KEY,
  csrf_token TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL
);
CREATE INDEX idx_web_session_expiry ON web_session(expires_at);

CREATE TABLE web_login_attempt (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  successful INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_web_login_attempt_source ON web_login_attempt(source,created_at DESC);
