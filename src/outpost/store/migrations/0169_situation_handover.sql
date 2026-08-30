CREATE TABLE situation_snapshot (
  id INTEGER PRIMARY KEY,
  capability TEXT NOT NULL
    CHECK(capability IN ('public','member','responder','operator')),
  digest TEXT NOT NULL,
  facts_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX idx_situation_snapshot_capability
ON situation_snapshot(capability,id DESC);

CREATE INDEX idx_situation_snapshot_since
ON situation_snapshot(capability,created_at DESC,id DESC);

CREATE TABLE web_read_marker (
  account_id INTEGER NOT NULL REFERENCES web_account(id) ON DELETE CASCADE,
  scope TEXT NOT NULL,
  last_seen_at INTEGER NOT NULL,
  last_seen_id INTEGER,
  PRIMARY KEY(account_id,scope)
);

-- The prior row was a shared, lossy cursor. It cannot be assigned honestly to any viewer.
DELETE FROM kv WHERE ns='situation_snapshot';
