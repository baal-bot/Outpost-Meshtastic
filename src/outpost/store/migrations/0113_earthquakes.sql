CREATE TABLE earthquake (
  id INTEGER PRIMARY KEY,
  usgs_id TEXT NOT NULL UNIQUE,
  magnitude REAL NOT NULL,
  place TEXT NOT NULL,
  occurred_at INTEGER NOT NULL,
  source_updated_at INTEGER NOT NULL,
  longitude REAL NOT NULL,
  latitude REAL NOT NULL,
  depth_km REAL NOT NULL,
  distance_km REAL NOT NULL,
  bearing_deg INTEGER NOT NULL,
  significance INTEGER NOT NULL DEFAULT 0,
  usgs_url TEXT,
  review_state TEXT NOT NULL CHECK(review_state IN ('observed','pending','approved','dismissed')),
  linked_alert_id INTEGER REFERENCES alert(id) ON DELETE SET NULL,
  first_seen_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX idx_earthquake_recent ON earthquake(occurred_at DESC);
CREATE INDEX idx_earthquake_review ON earthquake(review_state,occurred_at DESC);
