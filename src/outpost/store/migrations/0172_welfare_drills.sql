CREATE TABLE responder_group (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK(length(name) BETWEEN 1 AND 50),
  response_type TEXT NOT NULL DEFAULT 'general'
    CHECK(response_type IN ('general','medical','fire','search','logistics','communications','public_safety')),
  created_at INTEGER NOT NULL,
  created_by TEXT NOT NULL
);

CREATE TABLE responder_group_member (
  group_id INTEGER NOT NULL REFERENCES responder_group(id) ON DELETE CASCADE,
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  added_at INTEGER NOT NULL,
  added_by TEXT NOT NULL,
  PRIMARY KEY(group_id,member_id)
);
CREATE INDEX idx_responder_group_member_member
  ON responder_group_member(member_id,group_id);

CREATE TABLE welfare_schedule (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  cadence TEXT NOT NULL CHECK(cadence IN ('weekly','biweekly','monthly')),
  day_of_period INTEGER NOT NULL CHECK(day_of_period BETWEEN 0 AND 28),
  local_time TEXT NOT NULL CHECK(length(local_time)=5),
  roster_policy TEXT NOT NULL
    CHECK(roster_policy IN ('all','responders','subscribed')),
  responder_group_id INTEGER REFERENCES responder_group(id) ON DELETE SET NULL,
  window_minutes INTEGER NOT NULL DEFAULT 120 CHECK(window_minutes BETWEEN 30 AND 1440),
  suppress_if_real_event INTEGER NOT NULL DEFAULT 1 CHECK(suppress_if_real_event IN (0,1)),
  recipient_limit INTEGER NOT NULL CHECK(recipient_limit >= 0),
  airtime_limit_ms INTEGER NOT NULL CHECK(airtime_limit_ms >= 0),
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  next_run_at INTEGER NOT NULL,
  last_run_at INTEGER,
  last_outcome TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  archived_at INTEGER,
  CHECK((cadence IN ('weekly','biweekly') AND day_of_period BETWEEN 0 AND 6)
     OR (cadence='monthly' AND day_of_period BETWEEN 1 AND 28)),
  CHECK((responder_group_id IS NULL) OR roster_policy='responders')
);
CREATE INDEX idx_welfare_schedule_due
  ON welfare_schedule(enabled,next_run_at);

ALTER TABLE watch_event ADD COLUMN event_kind TEXT NOT NULL DEFAULT 'real'
  CHECK(event_kind IN ('real','drill'));
ALTER TABLE watch_event ADD COLUMN responder_group_id INTEGER
  REFERENCES responder_group(id) ON DELETE SET NULL;
ALTER TABLE watch_event ADD COLUMN schedule_id INTEGER
  REFERENCES welfare_schedule(id) ON DELETE SET NULL;
ALTER TABLE watch_event ADD COLUMN scheduled_for INTEGER;
ALTER TABLE watch_event ADD COLUMN auto_close_at INTEGER;

CREATE TABLE welfare_event_roster (
  event_id INTEGER NOT NULL REFERENCES watch_event(id) ON DELETE CASCADE,
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  last_seen_at_open INTEGER NOT NULL,
  PRIMARY KEY(event_id,member_id)
);
CREATE INDEX idx_welfare_event_roster_member
  ON welfare_event_roster(member_id,event_id);

CREATE TABLE welfare_schedule_run (
  id INTEGER PRIMARY KEY,
  schedule_id INTEGER NOT NULL REFERENCES welfare_schedule(id) ON DELETE CASCADE,
  event_id INTEGER REFERENCES watch_event(id) ON DELETE SET NULL,
  due_at INTEGER NOT NULL,
  processed_at INTEGER NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN (
    'started','no_recipients','suppressed_real_event','suppressed_open_event',
    'suppressed_quiet_hours','suppressed_airtime_growth','failed'
  )),
  recipient_count INTEGER NOT NULL DEFAULT 0,
  detail TEXT,
  UNIQUE(schedule_id,due_at)
);
CREATE INDEX idx_welfare_schedule_run_time
  ON welfare_schedule_run(processed_at DESC);
