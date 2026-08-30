ALTER TABLE message_log ADD COLUMN to_mesh_id TEXT;
ALTER TABLE message_log ADD COLUMN payload BLOB;
ALTER TABLE message_log ADD COLUMN want_ack INTEGER;
ALTER TABLE message_log ADD COLUMN pki_encrypted INTEGER;
ALTER TABLE message_log ADD COLUMN pki_public_key BLOB;
ALTER TABLE message_log ADD COLUMN no_reply INTEGER;
ALTER TABLE message_log ADD COLUMN request_id INTEGER;
ALTER TABLE message_log ADD COLUMN routing_error TEXT;
ALTER TABLE message_log ADD COLUMN latitude REAL;
ALTER TABLE message_log ADD COLUMN longitude REAL;
ALTER TABLE message_log ADD COLUMN rx_time INTEGER;

CREATE INDEX idx_msglog_replay_range
ON message_log(direction, created_at, id);
