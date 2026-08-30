CREATE INDEX idx_web_login_attempt_account
ON web_login_attempt(username,successful,created_at DESC);

CREATE INDEX idx_web_login_attempt_failures
ON web_login_attempt(successful,created_at DESC);
