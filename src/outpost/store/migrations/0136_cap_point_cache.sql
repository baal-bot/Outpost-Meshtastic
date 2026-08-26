CREATE TABLE cap_point_cache (
  cache_key TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  query_lat REAL NOT NULL,
  query_lon REAL NOT NULL,
  service_area TEXT,
  status TEXT NOT NULL
    CHECK(status IN ('ok','empty','unsupported_region')),
  result_json TEXT NOT NULL,
  provider_timestamp TEXT,
  fetched_at INTEGER NOT NULL
);

CREATE INDEX idx_cap_point_cache_fetched ON cap_point_cache(fetched_at);
