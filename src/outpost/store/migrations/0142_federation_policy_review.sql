ALTER TABLE fed_peer ADD COLUMN policy_applied_by TEXT;
ALTER TABLE fed_peer ADD COLUMN policy_applied_at INTEGER;
ALTER TABLE fed_peer ADD COLUMN policy_review_at INTEGER;

UPDATE fed_peer
SET policy_applied_by=COALESCE(approved_by, 'legacy:migration'),
    policy_applied_at=COALESCE(approved_at, created_at)
WHERE policy_configured=1;
