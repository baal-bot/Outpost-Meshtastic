CREATE TABLE cap_alert (
  id INTEGER PRIMARY KEY,
  identifier TEXT NOT NULL UNIQUE,
  sender TEXT,
  sent_at TEXT,
  msg_type TEXT NOT NULL,
  status TEXT NOT NULL,
  event TEXT NOT NULL,
  headline TEXT NOT NULL,
  description TEXT,
  area_desc TEXT,
  severity TEXT,
  urgency TEXT,
  certainty TEXT,
  effective_at TEXT,
  expires_at TEXT NOT NULL,
  references_text TEXT,
  decision TEXT NOT NULL CHECK(decision IN ('accepted','withheld')),
  gate_reasons TEXT NOT NULL,
  review_state TEXT NOT NULL DEFAULT 'pending'
    CHECK(review_state IN ('pending','approved','dismissed','expired')),
  linked_alert_id INTEGER REFERENCES alert(id) ON DELETE SET NULL,
  raw_json TEXT NOT NULL,
  first_seen_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX idx_cap_review ON cap_alert(review_state,decision,expires_at);
