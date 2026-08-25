CREATE TABLE digest_state (
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  cadence TEXT NOT NULL,
  last_thread_id INTEGER NOT NULL DEFAULT 0,
  last_sent_at INTEGER,
  PRIMARY KEY(member_id,cadence)
);

CREATE TABLE digest_delivery_log (
  id INTEGER PRIMARY KEY,
  member_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  cadence TEXT NOT NULL,
  thread_count INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_digest_delivery_member ON digest_delivery_log(member_id,cadence,created_at DESC);
