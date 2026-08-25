CREATE TABLE fed_peer (
  id INTEGER PRIMARY KEY,
  mesh_id TEXT NOT NULL UNIQUE,
  node_name TEXT,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending','pairing','active','paused','rejected')),
  protocol_version INTEGER NOT NULL DEFAULT 1,
  shared_secret BLOB,
  tx_counter INTEGER NOT NULL DEFAULT 0,
  rx_counter INTEGER NOT NULL DEFAULT 0,
  boards TEXT NOT NULL DEFAULT '[]',
  capabilities TEXT NOT NULL DEFAULT '{}',
  discovery_transports TEXT NOT NULL DEFAULT '[]',
  sync_incidents INTEGER NOT NULL DEFAULT 0,
  relay_mail INTEGER NOT NULL DEFAULT 0,
  relay_alerts INTEGER NOT NULL DEFAULT 0,
  auto_accept_alerts TEXT NOT NULL DEFAULT '[]',
  quota_items_per_hour INTEGER NOT NULL DEFAULT 200,
  quota_mail_per_hour INTEGER NOT NULL DEFAULT 20,
  last_sync_at INTEGER,
  last_seen_at INTEGER,
  paused_reason TEXT,
  approved_by TEXT,
  approved_at INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE fed_seen (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  uid TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('send','recv')),
  seen_at INTEGER NOT NULL,
  PRIMARY KEY(peer_id,uid,direction)
);
CREATE INDEX idx_fed_seen_age ON fed_seen(seen_at);

CREATE TABLE fed_cursor (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  stream TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('send','recv')),
  cursor TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(peer_id,stream,direction)
);

CREATE TABLE fed_outbox (
  id INTEGER PRIMARY KEY,
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  msg_type INTEGER NOT NULL,
  body BLOB NOT NULL,
  transport_hint TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  sent_at INTEGER
);
CREATE INDEX idx_fed_outbox_pending ON fed_outbox(sent_at,expires_at);
