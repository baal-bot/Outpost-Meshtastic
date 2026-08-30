CREATE TABLE member_data_request (
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE RESTRICT,
  request_type TEXT NOT NULL CHECK(request_type IN ('removal')),
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK(state IN ('pending','approved','rejected')),
  conversation_key TEXT UNIQUE,
  requested_at INTEGER NOT NULL,
  reviewed_at INTEGER,
  reviewed_by TEXT,
  review_reason TEXT,
  pseudonym TEXT
);

CREATE UNIQUE INDEX idx_member_data_request_pending
ON member_data_request(member_id,request_type) WHERE state='pending';

CREATE INDEX idx_member_data_request_queue
ON member_data_request(state,requested_at DESC,id DESC);

CREATE INDEX idx_mail_from_member ON mail(from_id,created_at DESC);
CREATE INDEX idx_post_author_member ON post(author_id,created_at DESC);
CREATE INDEX idx_incident_reporter_member ON incident(reporter_id,created_at DESC);
CREATE INDEX idx_incident_update_author_member ON incident_update(author_id,created_at DESC);
CREATE INDEX idx_message_log_peer_member ON message_log(peer_mesh_id,created_at DESC);
