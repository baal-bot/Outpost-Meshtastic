-- Member questions and generated answers are short-lived privacy data. The remaining
-- de-identified quality fields support longer-term safety and performance review.
ALTER TABLE ai_interaction ADD COLUMN content_purged_at INTEGER;
CREATE INDEX idx_ai_interaction_content_retention
ON ai_interaction(content_purged_at,created_at);

-- Relay events are immutable to application writers, but bounded maintenance must be able
-- to remove them after their documented history window. Updates remain prohibited.
DROP TRIGGER fed_relay_event_append_only_delete;
CREATE TRIGGER fed_relay_event_append_only_delete
BEFORE DELETE ON fed_relay_event
WHEN NOT EXISTS (
  SELECT 1 FROM runtime_setting
  WHERE key='maintenance.allow_relay_event_delete' AND value='true'
)
BEGIN
  SELECT RAISE(ABORT,'relay event history is append-only');
END;
