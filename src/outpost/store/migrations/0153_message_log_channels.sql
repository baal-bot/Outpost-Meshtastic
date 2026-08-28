CREATE INDEX idx_msglog_channel_time ON message_log(channel, created_at DESC);
