CREATE TABLE web_account (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL COLLATE NOCASE UNIQUE,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('administrator','operator','viewer')),
  password_hash TEXT NOT NULL,
  must_change INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  bootstrap_expires_at INTEGER,
  bootstrap_consumed_at INTEGER,
  totp_secret TEXT,
  totp_pending_secret TEXT,
  totp_confirmed_at INTEGER,
  recovery_code_hashes TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL,
  changed_at INTEGER,
  last_login_at INTEGER,
  created_by TEXT
);

INSERT INTO web_account(
  id,username,display_name,role,password_hash,must_change,enabled,
  bootstrap_expires_at,bootstrap_consumed_at,created_at,changed_at,created_by
)
SELECT id,'operator','Operator','administrator',password_hash,must_change,1,
       bootstrap_expires_at,bootstrap_consumed_at,created_at,changed_at,'migration:0145'
FROM web_credential WHERE id=1;

ALTER TABLE web_session ADD COLUMN account_id INTEGER REFERENCES web_account(id);
ALTER TABLE web_session ADD COLUMN source TEXT;
ALTER TABLE web_session ADD COLUMN user_agent TEXT;
ALTER TABLE web_session ADD COLUMN step_up_until INTEGER;
UPDATE web_session SET account_id=1 WHERE account_id IS NULL;
CREATE INDEX idx_web_session_account ON web_session(account_id,last_seen_at DESC);

ALTER TABLE web_login_attempt ADD COLUMN username TEXT NOT NULL DEFAULT 'operator';
CREATE INDEX idx_web_login_attempt_identity
ON web_login_attempt(source,username,created_at DESC);
