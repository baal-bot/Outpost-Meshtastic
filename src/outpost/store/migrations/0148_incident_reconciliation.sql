ALTER TABLE incident ADD COLUMN merged_into_id INTEGER REFERENCES incident(id);
ALTER TABLE incident ADD COLUMN reconciliation_review INTEGER NOT NULL DEFAULT 0
  CHECK(reconciliation_review IN (0,1));
CREATE INDEX idx_incident_canonical ON incident(merged_into_id,status,updated_at DESC);

CREATE TABLE incident_origin (
  origin_uid TEXT PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incident(id),
  original_incident_id INTEGER NOT NULL REFERENCES incident(id),
  origin_node TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('local','federation')),
  source_peer_id INTEGER REFERENCES fed_peer(id) ON DELETE SET NULL,
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  source_updated_at INTEGER NOT NULL,
  source_digest TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_incident_origin_current ON incident_origin(incident_id,origin_node);
CREATE INDEX idx_incident_origin_original ON incident_origin(original_incident_id);

CREATE TABLE incident_provenance (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incident(id),
  origin_uid TEXT NOT NULL,
  source_node TEXT NOT NULL,
  event_kind TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  source_updated_at INTEGER,
  recorded_at INTEGER NOT NULL,
  actor TEXT NOT NULL
);
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
BEGIN
  SELECT RAISE(ABORT,'incident provenance is append-only');
END;
CREATE TRIGGER incident_origin_identity_immutable
BEFORE UPDATE OF origin_uid,original_incident_id,origin_node,source_kind ON incident_origin
BEGIN
  SELECT RAISE(ABORT,'incident origin identity is immutable');
END;

CREATE TABLE incident_match_decision (
  source_incident_id INTEGER NOT NULL REFERENCES incident(id),
  target_incident_id INTEGER NOT NULL REFERENCES incident(id),
  state TEXT NOT NULL CHECK(state IN ('merged','rejected','unmerged')),
  score REAL NOT NULL,
  reasons_json TEXT NOT NULL,
  reviewed_at INTEGER NOT NULL,
  reviewed_by TEXT NOT NULL,
  PRIMARY KEY(source_incident_id,target_incident_id),
  CHECK(source_incident_id <> target_incident_id)
);

INSERT INTO incident_origin(
  origin_uid,incident_id,original_incident_id,origin_node,source_kind,
  first_seen_at,last_seen_at,source_updated_at
)
SELECT uid,id,id,origin_node,
       CASE WHEN uid LIKE '!%:%' THEN 'federation' ELSE 'local' END,
       created_at,updated_at,updated_at
FROM incident;

INSERT INTO incident_provenance(
  incident_id,origin_uid,source_node,event_kind,payload_json,
  source_updated_at,recorded_at,actor
)
SELECT id,uid,origin_node,'adopted',
       json_object(
         'type',type,'severity',severity,'status',status,'title',title,
         'body',body,'lat',lat,'lon',lon,'location_text',location_text,
         'expires_at',expires_at,'resolved_at',resolved_at,
         'resolution_note',resolution_note
       ),
       updated_at,updated_at,'migration:0148'
FROM incident;
