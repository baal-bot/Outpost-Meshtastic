ALTER TABLE fed_peer ADD COLUMN pairing_private BLOB;
ALTER TABLE fed_peer ADD COLUMN pairing_nonce BLOB;
ALTER TABLE fed_peer ADD COLUMN local_approved INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fed_peer ADD COLUMN remote_approved INTEGER NOT NULL DEFAULT 0;
