CREATE TABLE fed_post_delivery (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  post_id INTEGER NOT NULL REFERENCES post(id) ON DELETE CASCADE,
  uid TEXT NOT NULL,
  stream TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','sent','delivered')),
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  delivered_at INTEGER,
  error TEXT,
  PRIMARY KEY(peer_id,post_id)
);
CREATE UNIQUE INDEX idx_fed_post_delivery_uid ON fed_post_delivery(peer_id,uid);
CREATE INDEX idx_fed_post_delivery_pending ON fed_post_delivery(state,updated_at);
