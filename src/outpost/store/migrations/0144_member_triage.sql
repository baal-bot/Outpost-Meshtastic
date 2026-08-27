ALTER TABLE member ADD COLUMN directory_state TEXT NOT NULL DEFAULT 'active'
  CHECK(directory_state IN ('active','archived','ignored'));
ALTER TABLE member ADD COLUMN directory_state_at INTEGER;
ALTER TABLE member ADD COLUMN directory_state_by TEXT;
ALTER TABLE member ADD COLUMN reviewed_at INTEGER;
ALTER TABLE member ADD COLUMN reviewed_by TEXT;

CREATE INDEX idx_member_directory_triage
ON member(directory_state, trust, reviewed_at, last_seen DESC);

CREATE TABLE member_trust_history (
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE RESTRICT,
  from_trust TEXT NOT NULL,
  to_trust TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX idx_member_trust_history_member
ON member_trust_history(member_id, created_at DESC, id DESC);
