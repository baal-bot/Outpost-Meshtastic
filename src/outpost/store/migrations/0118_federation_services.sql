CREATE TABLE fed_service_request (
  request_id TEXT PRIMARY KEY,
  direction TEXT NOT NULL CHECK(direction IN ('out','in')),
  peer_mesh_id TEXT NOT NULL,
  service TEXT NOT NULL CHECK(service IN ('weather','alerts','knowledge')),
  args_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT,
  provenance_json TEXT,
  status TEXT NOT NULL CHECK(status IN ('pending','complete','failed','expired')),
  candidate_peers TEXT NOT NULL DEFAULT '[]',
  attempt INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  completed_at INTEGER,
  error TEXT
);
CREATE INDEX idx_fed_service_status ON fed_service_request(status,updated_at);
