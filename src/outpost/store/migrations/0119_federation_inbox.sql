CREATE TABLE fed_inbox_item (
  id INTEGER PRIMARY KEY,
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  stream TEXT NOT NULL,
  uid TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending','imported','rejected')),
  received_at INTEGER NOT NULL,
  reviewed_at INTEGER,
  reviewed_by TEXT,
  rejection_reason TEXT,
  UNIQUE(peer_id,stream,uid)
);
CREATE INDEX idx_fed_inbox_state ON fed_inbox_item(state,received_at DESC);
