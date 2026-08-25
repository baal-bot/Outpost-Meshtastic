CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

CREATE TABLE member (
  id INTEGER PRIMARY KEY,
  mesh_id TEXT NOT NULL UNIQUE,
  mesh_num INTEGER NOT NULL UNIQUE,
  handle TEXT UNIQUE,
  long_name TEXT,
  short_name TEXT,
  hw_model TEXT,
  public_key BLOB,
  trust TEXT NOT NULL DEFAULT 'guest',
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  last_heard_snr REAL,
  hops_away INTEGER,
  muted_until INTEGER,
  blocked_until INTEGER,
  unreachable_since INTEGER,
  prefs TEXT NOT NULL DEFAULT '{"position":"coarse"}',
  notes TEXT,
  handle_changed_at INTEGER,
  prior_handle TEXT,
  prior_handle_until INTEGER
);
CREATE INDEX idx_member_last_seen ON member(last_seen DESC);
CREATE INDEX idx_member_handle ON member(handle);

CREATE TABLE message_log (
  id INTEGER PRIMARY KEY,
  direction TEXT NOT NULL,
  member_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  peer_mesh_id TEXT,
  channel INTEGER NOT NULL,
  portnum INTEGER NOT NULL,
  is_direct INTEGER NOT NULL,
  packet_id INTEGER,
  text TEXT,
  byte_len INTEGER NOT NULL,
  toa_ms INTEGER,
  airtime_class TEXT,
  command TEXT,
  outcome TEXT,
  drop_reason TEXT,
  latency_ms INTEGER,
  rx_snr REAL,
  rx_rssi INTEGER,
  hops INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_msglog_time ON message_log(created_at DESC);
CREATE INDEX idx_msglog_member ON message_log(member_id, created_at DESC);
CREATE INDEX idx_msglog_command ON message_log(command, created_at DESC);

CREATE TABLE kv (
  ns TEXT NOT NULL,
  k TEXT NOT NULL,
  v TEXT NOT NULL,
  expires_at INTEGER,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (ns, k)
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  actor_kind TEXT NOT NULL,
  actor_ref TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  detail TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_audit_time ON audit_log(created_at DESC);

