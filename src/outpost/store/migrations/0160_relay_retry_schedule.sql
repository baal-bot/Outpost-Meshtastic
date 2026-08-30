ALTER TABLE fed_relay_envelope ADD COLUMN next_attempt_at INTEGER;

CREATE INDEX idx_fed_relay_retry
ON fed_relay_envelope(state,next_attempt_at,expires_at,created_at);
