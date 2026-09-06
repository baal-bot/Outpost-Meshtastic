-- Plain update notes have their own producer identities, not timestamp cursors.
-- A remote note is never republished under the receiver's producer lineage.
ALTER TABLE incident_update ADD COLUMN source_node TEXT;
ALTER TABLE incident_update ADD COLUMN origin_incident_uid TEXT;
ALTER TABLE incident_update ADD COLUMN source_epoch TEXT;
ALTER TABLE incident_update ADD COLUMN source_revision INTEGER;

UPDATE incident_update SET origin_incident_uid=(
  SELECT uid FROM incident WHERE id=incident_update.incident_id
);
INSERT INTO fed_revision(stream,uid)
  SELECT 'incident_updates',u.uid FROM incident_update u
  WHERE u.kind='update' AND u.uid NOT LIKE '!%:%'
    AND u.origin_incident_uid NOT LIKE '!%:%' ORDER BY u.id;

CREATE TRIGGER incident_update_identity_immutable BEFORE UPDATE OF
  uid,incident_id,source_node,origin_incident_uid ON incident_update
  WHEN NEW.uid IS NOT OLD.uid OR NEW.incident_id IS NOT OLD.incident_id
    OR NEW.source_node IS NOT OLD.source_node
    OR (OLD.origin_incident_uid IS NOT NULL
        AND NEW.origin_incident_uid IS NOT OLD.origin_incident_uid)
BEGIN
  SELECT RAISE(ABORT,'incident note identity is immutable');
END;
CREATE TRIGGER fed_revision_note_insert AFTER INSERT ON incident_update BEGIN
  UPDATE incident_update SET origin_incident_uid=(
    SELECT uid FROM incident WHERE id=NEW.incident_id
  ) WHERE id=NEW.id AND origin_incident_uid IS NULL;
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'incident_updates',NEW.uid FROM incident WHERE id=NEW.incident_id
      AND uid NOT LIKE '!%:%' AND NEW.uid NOT LIKE '!%:%'
      AND NEW.source_node IS NULL AND NEW.kind='update';
END;
CREATE TRIGGER fed_revision_note_update AFTER UPDATE OF
  kind,body,author_label,created_at,lat,lon ON incident_update
  WHEN NEW.source_node IS NULL AND NEW.uid NOT LIKE '!%:%'
    AND NEW.origin_incident_uid NOT LIKE '!%:%'
    AND (NEW.kind='update' OR OLD.kind='update')
BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid) VALUES('incident_updates',NEW.uid);
END;
CREATE TRIGGER fed_revision_note_delete AFTER DELETE ON incident_update
  WHEN OLD.source_node IS NULL AND OLD.uid NOT LIKE '!%:%'
    AND OLD.origin_incident_uid NOT LIKE '!%:%' AND OLD.kind='update'
BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid) VALUES('incident_updates',OLD.uid);
END;

-- A parent correction can make retained notes newly eligible for geographic
-- export, even when the note text itself did not change.
CREATE TRIGGER fed_revision_note_parent_location AFTER UPDATE OF lat,lon ON incident
  WHEN NEW.lat IS NOT OLD.lat OR NEW.lon IS NOT OLD.lon
BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'incident_updates',uid FROM incident_update WHERE incident_id=NEW.id
      AND source_node IS NULL AND kind='update' AND uid NOT LIKE '!%:%'
      AND origin_incident_uid NOT LIKE '!%:%';
END;
