ALTER TABLE fed_peer ADD COLUMN service_permissions TEXT NOT NULL DEFAULT '[]';
ALTER TABLE fed_peer ADD COLUMN quota_services_per_hour INTEGER NOT NULL DEFAULT 6
  CHECK(quota_services_per_hour BETWEEN 1 AND 60);
ALTER TABLE fed_peer ADD COLUMN service_concurrency INTEGER NOT NULL DEFAULT 1
  CHECK(service_concurrency BETWEEN 1 AND 4);
ALTER TABLE fed_peer ADD COLUMN service_max_response_bytes INTEGER NOT NULL DEFAULT 1200
  CHECK(service_max_response_bytes BETWEEN 256 AND 1600);
ALTER TABLE fed_peer ADD COLUMN service_airtime_seconds_per_hour REAL NOT NULL DEFAULT 15
  CHECK(service_airtime_seconds_per_hour BETWEEN 1 AND 120);

ALTER TABLE fed_service_request ADD COLUMN args_fingerprint TEXT;
ALTER TABLE fed_service_request ADD COLUMN response_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fed_service_request ADD COLUMN response_airtime_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE fed_service_request ADD COLUMN response_count INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_fed_service_peer_window
ON fed_service_request(peer_mesh_id,direction,created_at);
CREATE INDEX idx_fed_service_fingerprint
ON fed_service_request(peer_mesh_id,service,args_fingerprint,completed_at);

CREATE TABLE fed_service_usage (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  window_start INTEGER NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  denied INTEGER NOT NULL DEFAULT 0,
  response_bytes INTEGER NOT NULL DEFAULT 0,
  response_airtime_seconds REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(peer_id,window_start)
);

CREATE TABLE fed_service_circuit (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  service TEXT NOT NULL CHECK(service IN ('weather','alerts','knowledge')),
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  open_until INTEGER,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(peer_id,service)
);

-- Existing pairings need one explicit operator review for the newly introduced permissions.
UPDATE fed_peer SET policy_configured=0 WHERE state IN ('active','paused');
