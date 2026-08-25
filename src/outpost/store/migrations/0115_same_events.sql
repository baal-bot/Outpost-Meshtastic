CREATE TABLE same_event (
  id INTEGER PRIMARY KEY,
  header TEXT NOT NULL UNIQUE,
  originator TEXT NOT NULL,
  event_code TEXT NOT NULL,
  event_name TEXT NOT NULL,
  location_codes TEXT NOT NULL,
  purge_minutes INTEGER NOT NULL,
  issued_day INTEGER NOT NULL,
  issued_time TEXT NOT NULL,
  callsign TEXT NOT NULL,
  is_test INTEGER NOT NULL,
  relevant INTEGER NOT NULL,
  received_at INTEGER NOT NULL
);
CREATE INDEX idx_same_event_received ON same_event(received_at DESC);
