ALTER TABLE mail ADD COLUMN conversation_key TEXT;
ALTER TABLE mail ADD COLUMN federation_conversation_id TEXT;
ALTER TABLE mail ADD COLUMN operator_read_at INTEGER;
ALTER TABLE mail ADD COLUMN archived_at INTEGER;
ALTER TABLE mail ADD COLUMN message_kind TEXT NOT NULL DEFAULT 'member'
  CHECK(message_kind IN ('member','system'));
ALTER TABLE mail ADD COLUMN mail_direction TEXT NOT NULL DEFAULT 'local'
  CHECK(mail_direction IN ('local','in','out'));
ALTER TABLE mail ADD COLUMN source_peer_mesh_id TEXT;
ALTER TABLE mail ADD COLUMN reply_recipient_handle TEXT;
ALTER TABLE mail ADD COLUMN participant_handle TEXT;
ALTER TABLE mail ADD COLUMN operator_actor TEXT;

UPDATE mail
SET conversation_key='legacy:' || uid,
    participant_handle=CASE
      WHEN lower(to_label)='operator' THEN from_label
      ELSE to_label
    END,
    operator_actor=CASE
      WHEN lower(from_label) LIKE 'operator@%' THEN from_label
      ELSE NULL
    END,
    source_peer_mesh_id=reply_peer_mesh_id,
    reply_recipient_handle=CASE
      WHEN reply_peer_mesh_id IS NOT NULL THEN
        lower(CASE
          WHEN instr(from_label, '@') > 1 THEN substr(from_label, 1, instr(from_label, '@') - 1)
          ELSE from_label
        END)
      ELSE NULL
    END,
    message_kind=CASE
      WHEN lower(to_label)='operator' AND lower(from_label) LIKE 'operator@%' THEN 'system'
      ELSE 'member'
    END,
    mail_direction=CASE WHEN reply_peer_mesh_id IS NOT NULL THEN 'in' ELSE 'local' END;

CREATE INDEX idx_mail_conversation ON mail(conversation_key,created_at,id);
CREATE INDEX idx_mail_operator_state ON mail(archived_at,operator_read_at,created_at DESC);
