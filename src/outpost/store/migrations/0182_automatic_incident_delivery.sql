-- Automatic policy is durable per source version, not an unbounded retry log.
ALTER TABLE fed_incident_handoff ADD COLUMN fresh_revision INTEGER;
ALTER TABLE fed_incident_intent ADD COLUMN lane TEXT NOT NULL DEFAULT 'backfill'
  CHECK(lane IN ('fresh','backfill'));
ALTER TABLE fed_incident_intent ADD COLUMN urgency_rank INTEGER NOT NULL DEFAULT 3;
ALTER TABLE fed_incident_intent ADD COLUMN scheduled_at INTEGER;
ALTER TABLE fed_incident_intent ADD COLUMN deadline_at INTEGER;
ALTER TABLE fed_incident_intent ADD COLUMN next_attempt_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fed_incident_intent ADD COLUMN application_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fed_incident_intent ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE fed_incident_intent ADD COLUMN delivery_reason TEXT;
CREATE INDEX idx_incident_delivery_due
  ON fed_incident_intent(peer_id,lane,next_attempt_at,urgency_rank,first_revision);
CREATE INDEX idx_fed_peer_incident_worker ON fed_peer(state,sync_incidents,id);

-- At most one retained exact receipt reply per peer/source identity.
CREATE TABLE fed_incident_receipt_reply (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  stream TEXT NOT NULL,
  uid TEXT NOT NULL,
  value_json TEXT NOT NULL,
  secret_digest TEXT NOT NULL,
  counter INTEGER NOT NULL,
  queue_key TEXT NOT NULL UNIQUE,
  frame_ids TEXT NOT NULL,
  PRIMARY KEY(peer_id,stream,uid)
);
