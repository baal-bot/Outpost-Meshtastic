CREATE TABLE env_cache (
  cache_key TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  payload TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  etag TEXT,
  last_modified TEXT
);
CREATE INDEX idx_env_cache_expiry ON env_cache(expires_at);
