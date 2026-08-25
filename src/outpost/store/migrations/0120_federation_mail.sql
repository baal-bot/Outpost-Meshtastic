CREATE TABLE fed_mail_delivery (
  relay_id TEXT PRIMARY KEY,
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  direction TEXT NOT NULL CHECK(direction IN ('out','in')),
  mail_id INTEGER REFERENCES mail(id) ON DELETE SET NULL,
  recipient_handle TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('queued','sent','delivered','failed','expired')),
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  error TEXT
);
CREATE INDEX idx_fed_mail_state ON fed_mail_delivery(state,updated_at);
