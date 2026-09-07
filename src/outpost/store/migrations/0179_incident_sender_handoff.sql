-- Per-peer source staging, not radio admission or a remote storage receipt.
-- Keep source journal rows: one peer's scan cannot consume another peer's work.
CREATE INDEX idx_incident_change_revision ON incident_change_event(revision);

CREATE TABLE fed_incident_handoff (
  peer_id INTEGER PRIMARY KEY REFERENCES fed_peer(id) ON DELETE CASCADE,
  producer_mesh_id TEXT NOT NULL,
  peer_mesh_id TEXT NOT NULL,
  epoch TEXT NOT NULL,
  scope TEXT NOT NULL,
  after_revision INTEGER NOT NULL CHECK(after_revision>=0),
  observed_revision INTEGER NOT NULL CHECK(observed_revision>=after_revision)
);

CREATE TABLE fed_incident_intent (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  stream TEXT NOT NULL CHECK(stream IN ('incidents','incident_updates')),
  uid TEXT NOT NULL,
  epoch TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>0),
  first_revision INTEGER NOT NULL CHECK(first_revision>0 AND first_revision<=revision),
  scope TEXT NOT NULL,
  digest TEXT,
  parent_uid TEXT,
  state TEXT NOT NULL CHECK(state IN ('pending','not_exportable','invalid_payload')),
  PRIMARY KEY(peer_id,stream,uid)
);
CREATE UNIQUE INDEX idx_fed_incident_intent_pending
  ON fed_incident_intent(peer_id,first_revision);
