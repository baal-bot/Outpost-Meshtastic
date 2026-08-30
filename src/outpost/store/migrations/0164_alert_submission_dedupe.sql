ALTER TABLE alert ADD COLUMN request_fingerprint TEXT;
CREATE INDEX idx_alert_request_dedupe
ON alert(request_fingerprint,raised_at DESC)
WHERE request_fingerprint IS NOT NULL;
