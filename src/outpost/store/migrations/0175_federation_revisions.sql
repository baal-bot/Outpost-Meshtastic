-- Producer-owned revisions, independent of wall time. Replacement allocates a new
-- AUTOINCREMENT sequence; one metadata-only head is retained per stream/identity.
-- Keep sqlite_sequence and the lineage across retention, restart and full backup.
CREATE TABLE fed_revision (
  revision INTEGER PRIMARY KEY AUTOINCREMENT,
  stream TEXT NOT NULL,
  uid TEXT NOT NULL,
  UNIQUE(stream,uid)
);
CREATE TABLE fed_revision_lineage (
  id INTEGER PRIMARY KEY CHECK(id=1),
  epoch TEXT NOT NULL
);
INSERT INTO fed_revision_lineage VALUES(1,lower(hex(randomblob(16))));

INSERT INTO fed_revision(stream,uid)
  SELECT 'board:'||b.slug,p.uid FROM post p JOIN thread t ON t.id=p.thread_id
  JOIN board b ON b.id=t.board_id ORDER BY p.id;
INSERT INTO fed_revision(stream,uid) SELECT 'incidents',uid FROM incident ORDER BY id;
INSERT INTO fed_revision(stream,uid) SELECT 'alerts',uid FROM alert ORDER BY id;

CREATE TRIGGER fed_revision_incident_insert AFTER INSERT ON incident BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid) VALUES('incidents',NEW.uid);
END;
CREATE TRIGGER fed_revision_incident_update AFTER UPDATE OF
  type,severity,status,title,body,lat,lon,location_text,radius_m,reporter_label,
  origin_node,created_at,updated_at,expires_at,resolved_at,resolution_note,merged_into_id
  ON incident BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid) VALUES('incidents',NEW.uid);
END;
CREATE TRIGGER fed_revision_incident_delete AFTER DELETE ON incident BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid) VALUES('incidents',OLD.uid);
END;
CREATE TRIGGER fed_revision_origin_insert AFTER INSERT ON incident_origin BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'incidents',uid FROM incident WHERE id=NEW.incident_id;
END;
CREATE TRIGGER fed_revision_origin_update AFTER UPDATE OF incident_id,origin_uid
  ON incident_origin BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'incidents',uid FROM incident WHERE id IN (OLD.incident_id,NEW.incident_id);
END;
CREATE TRIGGER fed_revision_origin_delete AFTER DELETE ON incident_origin BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'incidents',uid FROM incident WHERE id=OLD.incident_id;
END;

CREATE TRIGGER fed_revision_alert_insert AFTER INSERT ON alert BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid) VALUES('alerts',NEW.uid);
END;
CREATE TRIGGER fed_revision_alert_update AFTER UPDATE OF
  severity,headline,body,source,source_ref,raised_by,raised_at,effective_at,expires_at,cancelled_at
  ON alert BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid) VALUES('alerts',NEW.uid);
END;
CREATE TRIGGER fed_revision_alert_delete AFTER DELETE ON alert BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid) VALUES('alerts',OLD.uid);
END;

CREATE TRIGGER fed_revision_post_insert AFTER INSERT ON post BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'board:'||b.slug,NEW.uid FROM thread t JOIN board b ON b.id=t.board_id
    WHERE t.id=NEW.thread_id;
END;
CREATE TRIGGER fed_revision_post_update AFTER UPDATE OF
  uid,thread_id,seq,author_label,origin_node,body,created_at,edited_at,hidden ON post BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'board:'||b.slug,OLD.uid FROM thread t JOIN board b ON b.id=t.board_id
    WHERE t.id=OLD.thread_id;
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'board:'||b.slug,NEW.uid FROM thread t JOIN board b ON b.id=t.board_id
    WHERE t.id=NEW.thread_id;
END;
CREATE TRIGGER fed_revision_post_delete BEFORE DELETE ON post BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'board:'||b.slug,OLD.uid FROM thread t JOIN board b ON b.id=t.board_id
    WHERE t.id=OLD.thread_id;
END;
CREATE TRIGGER fed_revision_thread_update AFTER UPDATE OF uid,board_id,subject,hidden
  ON thread BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'board:'||b.slug,p.uid FROM post p JOIN board b ON b.id=OLD.board_id
    WHERE p.thread_id=NEW.id;
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'board:'||b.slug,p.uid FROM post p JOIN board b ON b.id=NEW.board_id
    WHERE p.thread_id=NEW.id;
END;
CREATE TRIGGER fed_revision_thread_delete BEFORE DELETE ON thread BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'board:'||b.slug,p.uid FROM post p JOIN board b ON b.id=OLD.board_id
    WHERE p.thread_id=OLD.id;
END;
CREATE TRIGGER fed_revision_board_update AFTER UPDATE OF slug,federated,archived ON board BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'board:'||OLD.slug,p.uid FROM post p JOIN thread t ON t.id=p.thread_id
    WHERE t.board_id=NEW.id;
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'board:'||NEW.slug,p.uid FROM post p JOIN thread t ON t.id=p.thread_id
    WHERE t.board_id=NEW.id;
END;
CREATE TRIGGER fed_revision_board_delete BEFORE DELETE ON board BEGIN
  INSERT OR REPLACE INTO fed_revision(stream,uid)
    SELECT 'board:'||OLD.slug,p.uid FROM post p JOIN thread t ON t.id=p.thread_id
    WHERE t.board_id=OLD.id;
END;

ALTER TABLE fed_inbox_item ADD COLUMN source_epoch TEXT;
ALTER TABLE fed_inbox_item ADD COLUMN source_revision INTEGER;
ALTER TABLE incident_origin ADD COLUMN source_epoch TEXT;
ALTER TABLE incident_origin ADD COLUMN source_revision INTEGER;
ALTER TABLE fed_peer ADD COLUMN reconciliation_version INTEGER NOT NULL DEFAULT 1
  CHECK(reconciliation_version IN (1,2));

-- Receipts survive reviewed-payload retention. Pending review is not approval.
CREATE TABLE fed_revision_receipt (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  stream TEXT NOT NULL,
  uid TEXT NOT NULL,
  epoch TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>0),
  digest TEXT NOT NULL,
  PRIMARY KEY(peer_id,stream,uid)
);
