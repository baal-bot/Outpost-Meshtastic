-- Older sends could commit the shared placeholder before assigning a final UID.
-- The original node ID is not stored on local mail, so use a distinct recovery
-- namespace instead of inventing an origin. Keep row IDs and existing thread keys
-- intact so replies and operator links survive. This data repair is idempotent.
UPDATE mail
SET uid='recovered:mail:' || lower(hex(randomblob(16)))
WHERE uid='pending';

UPDATE mail
SET conversation_key='local:' || uid
WHERE uid GLOB 'recovered:mail:*' AND conversation_key IS NULL;
