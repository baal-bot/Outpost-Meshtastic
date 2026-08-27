ALTER TABLE same_event ADD COLUMN expires_at INTEGER;
ALTER TABLE same_event ADD COLUMN decision TEXT NOT NULL DEFAULT 'log_only'
  CHECK(decision IN ('accepted','withheld','log_only','duplicate'));
ALTER TABLE same_event ADD COLUMN gate_reasons TEXT NOT NULL DEFAULT '[]';
ALTER TABLE same_event ADD COLUMN review_state TEXT NOT NULL DEFAULT 'logged'
  CHECK(review_state IN ('pending','approved','dismissed','expired','logged','duplicate'));
ALTER TABLE same_event ADD COLUMN cap_alert_id INTEGER REFERENCES cap_alert(id) ON DELETE SET NULL;
ALTER TABLE same_event ADD COLUMN linked_alert_id INTEGER REFERENCES alert(id) ON DELETE SET NULL;

CREATE INDEX idx_same_review ON same_event(review_state,decision,expires_at);
