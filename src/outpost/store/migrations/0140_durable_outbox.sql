CREATE TABLE outbound_work (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  batch_uid TEXT,
  state TEXT NOT NULL CHECK(state IN (
    'pending','held','sending','awaiting_ack','sent','acked','failed','expired',
    'cancelled','superseded','retracted'
  )),
  text TEXT NOT NULL DEFAULT '',
  binary_payload BLOB,
  destination TEXT NOT NULL,
  channel INTEGER NOT NULL,
  traffic_class TEXT NOT NULL,
  severity TEXT NOT NULL,
  want_ack INTEGER NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  supersedes TEXT,
  queue_key TEXT,
  dedupe_token TEXT,
  dedupe_hash TEXT NOT NULL,
  portnum INTEGER,
  multipart INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at REAL,
  next_attempt_at REAL,
  packet_id INTEGER,
  outcome TEXT,
  last_error TEXT,
  completed_at REAL
);

CREATE INDEX idx_outbound_work_state
ON outbound_work(state, traffic_class, created_at, id);

CREATE INDEX idx_outbound_work_dedupe
ON outbound_work(destination, channel, dedupe_hash, created_at DESC);

CREATE INDEX idx_outbound_work_packet
ON outbound_work(packet_id) WHERE packet_id IS NOT NULL;

CREATE TABLE outbound_attempt (
  id INTEGER PRIMARY KEY,
  outbox_id INTEGER NOT NULL REFERENCES outbound_work(id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('started','sent','uncertain')),
  started_at REAL NOT NULL,
  completed_at REAL,
  estimated_toa_ms INTEGER NOT NULL,
  packet_id INTEGER,
  outcome TEXT,
  error TEXT,
  message_log_id INTEGER,
  UNIQUE(outbox_id, attempt_no)
);

CREATE INDEX idx_outbound_attempt_airtime
ON outbound_attempt(state, started_at);

ALTER TABLE message_log ADD COLUMN outbox_id INTEGER;

CREATE UNIQUE INDEX idx_message_log_outbox
ON message_log(outbox_id) WHERE outbox_id IS NOT NULL;
