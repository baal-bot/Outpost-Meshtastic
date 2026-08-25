CREATE TABLE alert (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  incident_id INTEGER REFERENCES incident(id) ON DELETE SET NULL,
  severity TEXT NOT NULL CHECK(severity IN ('caution','urgent','critical')),
  headline TEXT NOT NULL CHECK(length(CAST(headline AS BLOB)) <= 140),
  body TEXT,
  source TEXT NOT NULL CHECK(source IN ('operator','incident','cap','same')),
  source_ref TEXT,
  channels TEXT NOT NULL,
  raised_by TEXT NOT NULL,
  raised_at INTEGER NOT NULL,
  effective_at INTEGER,
  expires_at INTEGER,
  cancelled_at INTEGER,
  escalation_stage INTEGER NOT NULL DEFAULT 0,
  next_escalation_at INTEGER,
  ack_required INTEGER NOT NULL DEFAULT 0,
  broadcast_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_alert_active ON alert(expires_at,cancelled_at);

CREATE TABLE alert_ack (
  alert_id INTEGER NOT NULL REFERENCES alert(id) ON DELETE CASCADE,
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  acked_at INTEGER NOT NULL,
  note TEXT,
  PRIMARY KEY(alert_id,member_id)
);
