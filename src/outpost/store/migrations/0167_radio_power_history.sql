CREATE TABLE radio_power_sample (
  id INTEGER PRIMARY KEY,
  captured_at INTEGER NOT NULL,
  battery_level INTEGER CHECK(battery_level IS NULL OR battery_level BETWEEN 0 AND 100)
);

CREATE INDEX idx_radio_power_sample_time ON radio_power_sample(captured_at);
