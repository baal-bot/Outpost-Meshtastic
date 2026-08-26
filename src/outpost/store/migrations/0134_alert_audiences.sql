ALTER TABLE alert ADD COLUMN all_clear_at INTEGER;
ALTER TABLE alert ADD COLUMN all_clear_queued INTEGER NOT NULL DEFAULT 0;

CREATE TABLE alert_audience (
  alert_id INTEGER NOT NULL REFERENCES alert(id) ON DELETE CASCADE,
  destination TEXT NOT NULL,
  channel INTEGER NOT NULL CHECK(channel BETWEEN 0 AND 7),
  first_admitted_at INTEGER NOT NULL,
  last_admitted_at INTEGER NOT NULL,
  admissions INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(alert_id,destination,channel)
);
