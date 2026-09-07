-- Guard ownership survives process recovery; absent owner wiring denies dispatch.
ALTER TABLE outbound_work ADD COLUMN guard_kind TEXT;
CREATE INDEX idx_outbound_work_queue_key ON outbound_work(queue_key)
  WHERE queue_key IS NOT NULL;

-- One current association per peer/source identity, not an unbounded attempt log.
CREATE TABLE fed_incident_dispatch (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  stream TEXT NOT NULL CHECK(stream IN ('incidents','incident_updates')),
  uid TEXT NOT NULL,
  producer_mesh_id TEXT NOT NULL,
  peer_mesh_id TEXT NOT NULL,
  epoch TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>0),
  digest TEXT NOT NULL,
  scope TEXT NOT NULL,
  secret_digest TEXT NOT NULL,
  queue_key TEXT NOT NULL UNIQUE,
  counter INTEGER NOT NULL CHECK(counter BETWEEN 1 AND 4294967295),
  frame_ids TEXT NOT NULL,
  stored_at INTEGER,
  PRIMARY KEY(peer_id,stream,uid)
);
