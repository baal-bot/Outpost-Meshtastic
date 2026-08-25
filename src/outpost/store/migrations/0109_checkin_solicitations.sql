CREATE TABLE checkin_solicitation (
  event_id INTEGER NOT NULL REFERENCES watch_event(id) ON DELETE CASCADE,
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  queue_item_id INTEGER NOT NULL,
  queued_at INTEGER NOT NULL,
  PRIMARY KEY (event_id, member_id)
);

CREATE INDEX idx_checkin_solicitation_event
  ON checkin_solicitation(event_id, queued_at);
