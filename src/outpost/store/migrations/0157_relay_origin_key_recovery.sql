ALTER TABLE fed_relay_identity ADD COLUMN rotation_from_public_key BLOB
  CHECK(rotation_from_public_key IS NULL OR length(rotation_from_public_key)=32);
ALTER TABLE fed_relay_identity ADD COLUMN rotation_signature BLOB
  CHECK(rotation_signature IS NULL OR length(rotation_signature)=64);
ALTER TABLE fed_relay_identity ADD COLUMN rotated_at INTEGER;
ALTER TABLE fed_relay_identity ADD COLUMN rotated_by TEXT;

ALTER TABLE fed_relay_envelope ADD COLUMN rotation_from_public_key BLOB
  CHECK(rotation_from_public_key IS NULL OR length(rotation_from_public_key)=32);
ALTER TABLE fed_relay_envelope ADD COLUMN rotation_signature BLOB
  CHECK(rotation_signature IS NULL OR length(rotation_signature)=64);

CREATE TABLE fed_relay_origin_candidate (
  origin_node TEXT NOT NULL,
  public_key BLOB NOT NULL CHECK(length(public_key)=32),
  fingerprint TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'observed'
    CHECK(state IN ('observed','trusted','rejected')),
  first_observed_from_peer_id INTEGER REFERENCES fed_peer(id) ON DELETE SET NULL,
  last_observed_from_peer_id INTEGER REFERENCES fed_peer(id) ON DELETE SET NULL,
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  observation_count INTEGER NOT NULL DEFAULT 1 CHECK(observation_count>=1),
  reviewed_at INTEGER,
  reviewed_by TEXT,
  PRIMARY KEY(origin_node,fingerprint)
);
CREATE INDEX idx_fed_relay_origin_candidate_state
ON fed_relay_origin_candidate(state,last_seen_at DESC);

-- Pre-upgrade multi-hop observations were incorrectly installed as authoritative pins.
-- Preserve them as review candidates unless the presenting peer was the claimed origin.
INSERT INTO fed_relay_origin_candidate(
  origin_node,public_key,fingerprint,state,first_observed_from_peer_id,
  last_observed_from_peer_id,first_seen_at,last_seen_at,observation_count
)
SELECT k.origin_node,k.public_key,k.fingerprint,'observed',k.observed_from_peer_id,
       k.observed_from_peer_id,k.first_seen_at,k.first_seen_at,1
FROM fed_relay_origin_key k
WHERE k.state='observed'
  AND NOT EXISTS (
    SELECT 1 FROM fed_peer p
    WHERE p.id=k.observed_from_peer_id AND lower(p.mesh_id)=lower(k.origin_node)
  );

DELETE FROM fed_relay_origin_key
WHERE state='observed'
  AND NOT EXISTS (
    SELECT 1 FROM fed_peer p
    WHERE p.id=fed_relay_origin_key.observed_from_peer_id
      AND lower(p.mesh_id)=lower(fed_relay_origin_key.origin_node)
  );
