CREATE TABLE safety_floor_attempt (
  member_mesh_id TEXT NOT NULL,
  command TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  accepted_at INTEGER NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 1,
  accepted_count INTEGER NOT NULL DEFAULT 1,
  coalesced_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(member_mesh_id, command, fingerprint)
);

CREATE INDEX idx_safety_floor_last_seen
  ON safety_floor_attempt(last_seen_at DESC);
