-- Incident history is retained as one unit. Child evidence follows the incident after the
-- retention window, while provenance remains immutable for every live parent.
CREATE TABLE incident_origin_retention (
  origin_uid TEXT PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  original_incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  origin_node TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('local','federation')),
  source_peer_id INTEGER REFERENCES fed_peer(id) ON DELETE SET NULL,
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  source_updated_at INTEGER NOT NULL,
  source_digest TEXT NOT NULL DEFAULT ''
);
INSERT INTO incident_origin_retention SELECT * FROM incident_origin;
DROP TABLE incident_origin;
ALTER TABLE incident_origin_retention RENAME TO incident_origin;
CREATE INDEX idx_incident_origin_current ON incident_origin(incident_id,origin_node);
CREATE INDEX idx_incident_origin_original ON incident_origin(original_incident_id);
CREATE TRIGGER incident_origin_identity_immutable
BEFORE UPDATE OF origin_uid,original_incident_id,origin_node,source_kind ON incident_origin
BEGIN
  SELECT RAISE(ABORT,'incident origin identity is immutable');
END;

CREATE TABLE incident_provenance_retention (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  origin_uid TEXT NOT NULL,
  source_node TEXT NOT NULL,
  event_kind TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  source_updated_at INTEGER,
  recorded_at INTEGER NOT NULL,
  actor TEXT NOT NULL
);
INSERT INTO incident_provenance_retention SELECT * FROM incident_provenance;
DROP TABLE incident_provenance;
ALTER TABLE incident_provenance_retention RENAME TO incident_provenance;
CREATE INDEX idx_incident_provenance_timeline
ON incident_provenance(incident_id,recorded_at,id);
CREATE INDEX idx_incident_provenance_origin
ON incident_provenance(origin_uid,recorded_at,id);
CREATE TRIGGER incident_provenance_append_only_update
BEFORE UPDATE ON incident_provenance
BEGIN
  SELECT RAISE(ABORT,'incident provenance is append-only');
END;
CREATE TRIGGER incident_provenance_append_only_delete
BEFORE DELETE ON incident_provenance
WHEN EXISTS(SELECT 1 FROM incident WHERE id=OLD.incident_id)
BEGIN
  SELECT RAISE(ABORT,'incident provenance is append-only');
END;

CREATE TABLE incident_match_decision_retention (
  source_incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  target_incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  state TEXT NOT NULL CHECK(state IN ('merged','rejected','unmerged')),
  score REAL NOT NULL,
  reasons_json TEXT NOT NULL,
  reviewed_at INTEGER NOT NULL,
  reviewed_by TEXT NOT NULL,
  PRIMARY KEY(source_incident_id,target_incident_id),
  CHECK(source_incident_id <> target_incident_id)
);
INSERT INTO incident_match_decision_retention SELECT * FROM incident_match_decision;
DROP TABLE incident_match_decision;
ALTER TABLE incident_match_decision_retention RENAME TO incident_match_decision;

-- merged_into_id predates an explicit ON DELETE action. This trigger is its managed
-- SET NULL policy and prevents a canonical incident from blocking retention forever.
CREATE TRIGGER incident_detach_merged_children
BEFORE DELETE ON incident
BEGIN
  UPDATE incident SET merged_into_id=NULL WHERE merged_into_id=OLD.id;
END;
