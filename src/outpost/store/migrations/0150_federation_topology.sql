CREATE TABLE fed_topology_policy (
  peer_id INTEGER PRIMARY KEY REFERENCES fed_peer(id) ON DELETE CASCADE,
  share_location INTEGER NOT NULL DEFAULT 0 CHECK(share_location IN (0,1)),
  location_lat REAL CHECK(location_lat BETWEEN -90 AND 90),
  location_lon REAL CHECK(location_lon BETWEEN -180 AND 180),
  precision_km REAL NOT NULL DEFAULT 10 CHECK(precision_km BETWEEN 1 AND 100),
  updated_at INTEGER NOT NULL,
  updated_by TEXT NOT NULL,
  last_sent_at INTEGER,
  CHECK(share_location=0 OR (location_lat IS NOT NULL AND location_lon IS NOT NULL))
);

CREATE TABLE fed_topology_peer (
  peer_id INTEGER PRIMARY KEY REFERENCES fed_peer(id) ON DELETE CASCADE,
  location_shared INTEGER NOT NULL DEFAULT 0 CHECK(location_shared IN (0,1)),
  location_lat REAL CHECK(location_lat BETWEEN -90 AND 90),
  location_lon REAL CHECK(location_lon BETWEEN -180 AND 180),
  precision_km REAL CHECK(precision_km BETWEEN 1 AND 100),
  generated_at INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  CHECK(location_shared=0 OR (
    location_lat IS NOT NULL AND location_lon IS NOT NULL AND precision_km IS NOT NULL
  ))
);

CREATE TABLE fed_peer_tombstone (
  mesh_id TEXT PRIMARY KEY,
  node_name TEXT,
  forgotten_at INTEGER NOT NULL,
  forgotten_by TEXT NOT NULL
);
CREATE INDEX idx_fed_peer_tombstone_time ON fed_peer_tombstone(forgotten_at DESC);
