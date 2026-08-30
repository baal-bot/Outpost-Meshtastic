ALTER TABLE alert ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'pending'
  CHECK(delivery_state IN ('pending','delivered','empty_audience','refused'));
ALTER TABLE alert ADD COLUMN last_delivery_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE alert ADD COLUMN delivery_error_at INTEGER;

ALTER TABLE checkin ADD COLUMN notification_state TEXT
  CHECK(notification_state IS NULL OR notification_state IN ('delivered','empty_audience','refused'));
ALTER TABLE checkin ADD COLUMN notification_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE incident ADD COLUMN notification_state TEXT
  CHECK(notification_state IS NULL OR notification_state IN ('delivered','empty_audience','refused'));
ALTER TABLE incident ADD COLUMN notification_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE message_log ADD COLUMN in_reply_to_id INTEGER REFERENCES message_log(id) ON DELETE SET NULL;
CREATE INDEX idx_msglog_reply ON message_log(in_reply_to_id);
