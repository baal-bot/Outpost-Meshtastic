ALTER TABLE fed_peer ADD COLUMN incident_lat REAL CHECK(incident_lat BETWEEN -90 AND 90);
ALTER TABLE fed_peer ADD COLUMN incident_lon REAL CHECK(incident_lon BETWEEN -180 AND 180);
ALTER TABLE fed_peer ADD COLUMN incident_radius_km REAL NOT NULL DEFAULT 25
  CHECK(incident_radius_km BETWEEN 1 AND 500);
