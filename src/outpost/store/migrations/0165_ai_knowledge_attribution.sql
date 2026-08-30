ALTER TABLE kb_document ADD COLUMN created_by TEXT NOT NULL DEFAULT 'pre-migration';
ALTER TABLE kb_document ADD COLUMN updated_by TEXT NOT NULL DEFAULT 'pre-migration';

UPDATE kb_document
SET created_by=CASE WHEN source='seed' THEN 'seed' ELSE 'pre-migration' END,
    updated_by=CASE WHEN source='seed' THEN 'seed' ELSE 'pre-migration' END;

CREATE TABLE kb_document_tombstone (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL,
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  source TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  created_by TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  deleted_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted_at INTEGER NOT NULL
);
CREATE INDEX idx_kb_document_tombstone_document
ON kb_document_tombstone(document_id,deleted_at DESC);
