ALTER TABLE audit_log
ADD COLUMN outcome TEXT NOT NULL DEFAULT 'success'
CHECK (outcome IN ('success', 'denied', 'failure'));

CREATE INDEX idx_audit_outcome
ON audit_log(outcome, id DESC);
