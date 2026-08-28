ALTER TABLE web_account ADD COLUMN radio_member_id INTEGER
  REFERENCES member(id) ON DELETE SET NULL;
ALTER TABLE web_account ADD COLUMN radio_linked_at INTEGER;
ALTER TABLE web_account ADD COLUMN radio_linked_by TEXT;

CREATE UNIQUE INDEX idx_web_account_radio_member
ON web_account(radio_member_id) WHERE radio_member_id IS NOT NULL;
