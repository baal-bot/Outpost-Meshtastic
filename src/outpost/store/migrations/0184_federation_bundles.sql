-- A commissioned identity survives a radio-off restart; never infer it from a peer.
CREATE TABLE fed_bundle_identity (
  id INTEGER PRIMARY KEY CHECK(id=1),
  mesh_id TEXT NOT NULL,
  commissioned_at INTEGER NOT NULL,
  commissioned_by TEXT NOT NULL
);

-- Bounded by admission, not time pruning: old files must not regain admission.
CREATE TABLE fed_bundle_receipt (
  digest TEXT PRIMARY KEY CHECK(length(digest)=64),
  origin TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  imported_at INTEGER NOT NULL,
  imported_by TEXT NOT NULL,
  imported_count INTEGER NOT NULL,
  skipped_count INTEGER NOT NULL
);
