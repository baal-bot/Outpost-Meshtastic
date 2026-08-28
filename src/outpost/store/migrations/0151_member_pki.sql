ALTER TABLE member ADD COLUMN pending_public_key BLOB
  CHECK(pending_public_key IS NULL OR length(pending_public_key)=32);
ALTER TABLE member ADD COLUMN pki_state TEXT NOT NULL DEFAULT 'unknown'
  CHECK(pki_state IN ('unknown','pending','verified','conflict'));
ALTER TABLE member ADD COLUMN pki_verified_at INTEGER;
ALTER TABLE member ADD COLUMN pki_last_seen_at INTEGER;

CREATE TABLE member_pki_event (
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE RESTRICT,
  event TEXT NOT NULL CHECK(event IN (
    'observed','verified','conflict','rejected','elevated_denied','replay_denied'
  )),
  fingerprint TEXT,
  prior_fingerprint TEXT,
  actor TEXT NOT NULL,
  detail TEXT,
  created_at INTEGER NOT NULL
);

CREATE INDEX idx_member_pki_event_member
ON member_pki_event(member_id,created_at DESC,id DESC);

CREATE TABLE member_pki_replay (
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  packet_id INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  command TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  PRIMARY KEY(member_id,packet_id,fingerprint)
);

CREATE INDEX idx_member_pki_replay_time ON member_pki_replay(received_at);
