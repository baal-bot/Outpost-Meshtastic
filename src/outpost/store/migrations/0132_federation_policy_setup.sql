ALTER TABLE fed_peer ADD COLUMN policy_configured INTEGER NOT NULL DEFAULT 0
  CHECK(policy_configured IN (0,1));

-- Preserve policy decisions already made before the guided setup existed.
UPDATE fed_peer SET policy_configured=1
WHERE boards <> '[]' OR sync_incidents=1 OR relay_alerts=1 OR relay_mail=1;
