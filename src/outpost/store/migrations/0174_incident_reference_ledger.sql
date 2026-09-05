-- Keep reference-to-identity bindings for the lifetime of this database, including
-- after incident content is purged. NULL marks a retired ambiguous legacy number.
CREATE TABLE incident_reference (
  local_ref INTEGER PRIMARY KEY CHECK(local_ref > 0),
  incident_uid TEXT UNIQUE
);

INSERT INTO incident_reference(local_ref,incident_uid)
SELECT local_ref,CASE WHEN COUNT(*)=1 THEN MIN(uid) ELSE NULL END
FROM incident GROUP BY local_ref;

-- Previously reused numbers cannot safely select either incident. Retire those
-- numbers and give every affected record a fresh reference above the high-water mark.
INSERT INTO incident_reference(local_ref,incident_uid)
SELECT (SELECT COALESCE(MAX(local_ref),0) FROM incident_reference)
       + ROW_NUMBER() OVER (ORDER BY i.id),i.uid
FROM incident i JOIN incident_reference r ON r.local_ref=i.local_ref
WHERE r.incident_uid IS NULL;

UPDATE incident
SET local_ref=(SELECT r.local_ref FROM incident_reference r WHERE r.incident_uid=incident.uid)
WHERE local_ref IN (SELECT local_ref FROM incident_reference WHERE incident_uid IS NULL);

DROP INDEX idx_incident_active_ref;
CREATE UNIQUE INDEX idx_incident_ref ON incident(local_ref);

CREATE TRIGGER incident_reference_guard BEFORE INSERT ON incident
WHEN EXISTS (
  SELECT 1 FROM incident_reference r
  WHERE (r.local_ref=NEW.local_ref AND r.incident_uid IS NOT NEW.uid)
     OR (r.incident_uid=NEW.uid AND r.local_ref<>NEW.local_ref)
)
BEGIN
  SELECT RAISE(ABORT,'incident reference already reserved');
END;

CREATE TRIGGER incident_reference_register AFTER INSERT ON incident BEGIN
  INSERT OR IGNORE INTO incident_reference(local_ref,incident_uid) VALUES(NEW.local_ref,NEW.uid);
END;

CREATE TRIGGER incident_reference_immutable BEFORE UPDATE OF local_ref,uid ON incident
WHEN NEW.local_ref<>OLD.local_ref OR NEW.uid<>OLD.uid
BEGIN
  SELECT RAISE(ABORT,'incident reference identity is immutable');
END;

CREATE TRIGGER incident_reference_no_update BEFORE UPDATE ON incident_reference BEGIN
  SELECT RAISE(ABORT,'incident reference ledger is append-only');
END;
CREATE TRIGGER incident_reference_no_delete BEFORE DELETE ON incident_reference BEGIN
  SELECT RAISE(ABORT,'incident reference ledger is append-only');
END;
