CREATE TABLE incident (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  local_ref INTEGER NOT NULL,
  type TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('info','caution','urgent','critical')),
  status TEXT NOT NULL DEFAULT 'open'
    CHECK(status IN ('open','monitoring','resolved','false_alarm','expired')),
  title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 64),
  body TEXT,
  lat REAL, lon REAL, location_text TEXT, radius_m INTEGER,
  reporter_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  reporter_label TEXT NOT NULL,
  origin_node TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER,
  resolved_at INTEGER,
  resolved_by TEXT,
  resolution_note TEXT,
  confirm_count INTEGER NOT NULL DEFAULT 0,
  dispute_count INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'member',
  location_unconfirmed INTEGER NOT NULL DEFAULT 0,
  position_suppressed INTEGER NOT NULL DEFAULT 0,
  unverified INTEGER NOT NULL DEFAULT 0,
  flagged_for_review INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_incident_status ON incident(status,severity,updated_at DESC);
CREATE INDEX idx_incident_geo ON incident(lat,lon);
CREATE UNIQUE INDEX idx_incident_active_ref ON incident(local_ref)
  WHERE status IN ('open','monitoring');

CREATE TABLE incident_update (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  author_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  author_label TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('update','confirm','dispute','ack','status_change')),
  body TEXT,
  lat REAL, lon REAL,
  created_at INTEGER NOT NULL,
  UNIQUE(incident_id,seq)
);
CREATE INDEX idx_incident_update ON incident_update(incident_id,seq DESC);
