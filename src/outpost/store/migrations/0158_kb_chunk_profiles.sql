ALTER TABLE kb_document ADD COLUMN chunk_token_limit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE kb_document ADD COLUMN chunk_overlap_tokens INTEGER NOT NULL DEFAULT 0;
