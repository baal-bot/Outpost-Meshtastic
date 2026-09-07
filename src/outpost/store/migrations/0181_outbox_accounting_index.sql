-- Retain late/uncertain completions without scanning all historical attempts.
CREATE INDEX idx_outbound_attempt_accounting
ON outbound_attempt(state, MAX(started_at, COALESCE(completed_at, started_at)));
