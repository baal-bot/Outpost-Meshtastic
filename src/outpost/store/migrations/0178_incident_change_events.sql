-- Undispatched source work, NOT per-peer admission or delivery evidence. Attach
-- to the producer index so direct SQL, import, merge and retention writers share
-- the same commit/rollback boundary as IncidentService. No payload is copied.
CREATE TABLE incident_change_event (
  stream TEXT NOT NULL CHECK(stream IN ('incidents','incident_updates')),
  uid TEXT NOT NULL,
  epoch TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>0),
  first_revision INTEGER NOT NULL CHECK(first_revision>0 AND first_revision<=revision),
  PRIMARY KEY(stream,uid)
);
CREATE UNIQUE INDEX idx_incident_change_pending ON incident_change_event(first_revision);

-- Backfill only the latest known heads, including retained deletion heads. These
-- are not reconstructed historical events; first_revision starts at upgrade.
INSERT INTO incident_change_event(stream,uid,epoch,revision,first_revision)
  SELECT r.stream,r.uid,l.epoch,r.revision,r.revision
  FROM fed_revision r CROSS JOIN fed_revision_lineage l
  WHERE r.stream IN ('incidents','incident_updates');

CREATE TRIGGER incident_change_revision AFTER INSERT ON fed_revision
  WHEN NEW.stream IN ('incidents','incident_updates')
BEGIN
  INSERT INTO incident_change_event(stream,uid,epoch,revision,first_revision)
    VALUES(NEW.stream,NEW.uid,(SELECT epoch FROM fed_revision_lineage WHERE id=1),
      NEW.revision,NEW.revision)
    ON CONFLICT(stream,uid) DO UPDATE SET
      first_revision=CASE WHEN epoch=excluded.epoch THEN first_revision
        ELSE excluded.first_revision END,
      epoch=excluded.epoch,revision=excluded.revision;
END;
