-- Older NULL battery samples do not distinguish external power from missing data.
ALTER TABLE radio_power_sample ADD COLUMN external_power INTEGER NOT NULL DEFAULT 0
  CHECK(external_power IN (0, 1) AND (external_power = 0 OR battery_level IS NULL));
