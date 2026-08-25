CREATE TABLE watch_event (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  opened_at INTEGER NOT NULL,
  closed_at INTEGER,
  opened_by TEXT NOT NULL,
  roster_policy TEXT NOT NULL DEFAULT 'all'
    CHECK(roster_policy IN ('all','responders','subscribed'))
);
CREATE UNIQUE INDEX idx_watch_event_one_open ON watch_event((1)) WHERE closed_at IS NULL;

CREATE TABLE checkin (
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  event_id INTEGER REFERENCES watch_event(id) ON DELETE SET NULL,
  status TEXT NOT NULL CHECK(status IN ('ok','need_help','evacuated')),
  note TEXT,
  lat REAL,
  lon REAL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_checkin_member ON checkin(member_id,created_at DESC);
CREATE INDEX idx_checkin_event ON checkin(event_id,created_at DESC);
