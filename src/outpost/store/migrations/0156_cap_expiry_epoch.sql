ALTER TABLE cap_alert ADD COLUMN expires_epoch INTEGER;

-- SQLite's unixepoch understands ISO-8601 offsets and treats an omitted offset as UTC.
UPDATE cap_alert SET expires_epoch=unixepoch(expires_at);

-- Repair live alerts that the former byte-wise text comparison expired prematurely.
UPDATE cap_alert
SET review_state='pending'
WHERE review_state='expired' AND expires_epoch>unixepoch();

DROP INDEX idx_cap_review;
CREATE INDEX idx_cap_review ON cap_alert(review_state,decision,expires_epoch);
