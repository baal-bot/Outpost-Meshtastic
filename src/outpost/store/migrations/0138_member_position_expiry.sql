ALTER TABLE member_position
ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0;

-- Existing exact coordinates predate an explicit consent/retention schedule. Expire them on
-- upgrade; a new POS share will establish a fresh, operator-visible deletion time.
UPDATE member_position SET expires_at=received_at;

CREATE INDEX idx_member_position_expiry ON member_position(expires_at);
