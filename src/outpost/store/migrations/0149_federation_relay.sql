CREATE TABLE fed_relay_identity (
  id INTEGER PRIMARY KEY CHECK(id=1),
  private_key BLOB NOT NULL CHECK(length(private_key)=32),
  public_key BLOB NOT NULL CHECK(length(public_key)=32),
  created_at INTEGER NOT NULL
);

CREATE TABLE fed_relay_policy (
  peer_id INTEGER PRIMARY KEY REFERENCES fed_peer(id) ON DELETE CASCADE,
  enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
  paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
  scopes_json TEXT NOT NULL DEFAULT '[]',
  max_stored_items INTEGER NOT NULL DEFAULT 50
    CHECK(max_stored_items BETWEEN 1 AND 500),
  max_stored_bytes INTEGER NOT NULL DEFAULT 65536
    CHECK(max_stored_bytes BETWEEN 1024 AND 1048576),
  rate_per_hour INTEGER NOT NULL DEFAULT 20 CHECK(rate_per_hour BETWEEN 1 AND 200),
  airtime_seconds_per_hour REAL NOT NULL DEFAULT 30
    CHECK(airtime_seconds_per_hour BETWEEN 1 AND 300),
  updated_at INTEGER NOT NULL,
  updated_by TEXT NOT NULL
);

CREATE TABLE fed_relay_origin_key (
  origin_node TEXT PRIMARY KEY,
  public_key BLOB NOT NULL CHECK(length(public_key)=32),
  fingerprint TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('observed','trusted','rejected')),
  observed_from_peer_id INTEGER REFERENCES fed_peer(id) ON DELETE SET NULL,
  first_seen_at INTEGER NOT NULL,
  reviewed_at INTEGER,
  reviewed_by TEXT
);

CREATE TABLE fed_relay_envelope (
  envelope_id TEXT PRIMARY KEY,
  direction TEXT NOT NULL CHECK(direction IN ('origin','relay','destination')),
  origin_node TEXT NOT NULL,
  destination_node TEXT NOT NULL,
  scope TEXT NOT NULL CHECK(scope IN ('incident','request','receipt','opaque')),
  idempotency_key TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  hop_limit INTEGER NOT NULL CHECK(hop_limit BETWEEN 1 AND 4),
  payload_cbor BLOB,
  payload_bytes INTEGER NOT NULL,
  origin_public_key BLOB NOT NULL CHECK(length(origin_public_key)=32),
  origin_signature BLOB NOT NULL CHECK(length(origin_signature)=64),
  route_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN (
    'queued','quarantined','paused','forwarding','forwarded','delivered',
    'rejected','expired','purged'
  )),
  received_from_peer_id INTEGER REFERENCES fed_peer(id) ON DELETE SET NULL,
  received_transport TEXT CHECK(received_transport IN ('radio','mqtt')),
  next_hop_mesh_id TEXT,
  last_path TEXT CHECK(last_path IN ('direct','relay')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at INTEGER,
  receipt_sent_at INTEGER,
  stored_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_error TEXT,
  UNIQUE(origin_node,destination_node,idempotency_key)
);
CREATE INDEX idx_fed_relay_queue
ON fed_relay_envelope(state,expires_at,created_at);
CREATE INDEX idx_fed_relay_storage
ON fed_relay_envelope(received_from_peer_id,state,payload_bytes);

CREATE TABLE fed_relay_usage (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  window_start INTEGER NOT NULL,
  accepted INTEGER NOT NULL DEFAULT 0,
  denied INTEGER NOT NULL DEFAULT 0,
  bytes_stored INTEGER NOT NULL DEFAULT 0,
  forwarded INTEGER NOT NULL DEFAULT 0,
  airtime_seconds REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(peer_id,window_start)
);

CREATE TABLE fed_relay_event (
  id INTEGER PRIMARY KEY,
  envelope_id TEXT,
  peer_id INTEGER REFERENCES fed_peer(id) ON DELETE SET NULL,
  event_kind TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  actor TEXT NOT NULL
);
CREATE INDEX idx_fed_relay_event_time ON fed_relay_event(created_at DESC,id DESC);
CREATE TRIGGER fed_relay_event_append_only_update
BEFORE UPDATE ON fed_relay_event
BEGIN
  SELECT RAISE(ABORT,'relay event history is append-only');
END;
CREATE TRIGGER fed_relay_event_append_only_delete
BEFORE DELETE ON fed_relay_event
BEGIN
  SELECT RAISE(ABORT,'relay event history is append-only');
END;
