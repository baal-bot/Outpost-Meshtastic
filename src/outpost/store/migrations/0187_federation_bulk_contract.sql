-- One current pairing generation per peer. IP never consumes radio counters.
-- Only the latest exact inbound response is retained: a sender must commit that
-- response before issuing its next contiguous request in the same direction.
CREATE TABLE fed_bulk_state (
  peer_id INTEGER PRIMARY KEY REFERENCES fed_peer(id) ON DELETE CASCADE,
  local_mesh_id TEXT NOT NULL CHECK(length(local_mesh_id)=9),
  generation TEXT NOT NULL CHECK(length(generation)=64),
  configuration_digest TEXT NOT NULL CHECK(length(configuration_digest)=64),
  tx_sequence INTEGER NOT NULL DEFAULT 0 CHECK(tx_sequence>=0),
  rx_sequence INTEGER NOT NULL DEFAULT 0 CHECK(rx_sequence>=0),
  tx_request BLOB CHECK(tx_request IS NULL OR (typeof(tx_request)='blob' AND length(tx_request) BETWEEN 1 AND 196608)),
  tx_attempts INTEGER NOT NULL DEFAULT 0 CHECK(tx_attempts BETWEEN 0 AND 5),
  rx_request_digest TEXT CHECK(rx_request_digest IS NULL OR length(rx_request_digest)=64),
  rx_response BLOB CHECK(rx_response IS NULL OR (typeof(rx_response)='blob' AND length(rx_response) BETWEEN 1 AND 196608)),
  CHECK((rx_sequence=0 AND rx_request_digest IS NULL AND rx_response IS NULL) OR
        (rx_sequence>0 AND rx_request_digest IS NOT NULL AND rx_response IS NOT NULL)),
  CHECK(tx_request IS NULL OR tx_sequence>0),
  CHECK(tx_request IS NOT NULL OR tx_attempts=0)
);

-- Caps cover retained request/response bytes; fixed metadata is bounded by rows.
CREATE TRIGGER fed_bulk_insert_limit BEFORE INSERT ON fed_bulk_state
WHEN (SELECT count(*) FROM fed_bulk_state)>=32 OR
     (SELECT coalesce(sum(coalesce(length(tx_request),0)+coalesce(length(rx_response),0)),0)
      FROM fed_bulk_state)+coalesce(length(NEW.tx_request),0)+coalesce(length(NEW.rx_response),0)>8388608
BEGIN
  SELECT RAISE(ABORT, 'bulk storage full');
END;

CREATE TRIGGER fed_bulk_update_limit BEFORE UPDATE ON fed_bulk_state
WHEN (SELECT coalesce(sum(coalesce(length(tx_request),0)+coalesce(length(rx_response),0)),0)
      FROM fed_bulk_state WHERE peer_id<>OLD.peer_id)+
      coalesce(length(NEW.tx_request),0)+coalesce(length(NEW.rx_response),0)>8388608
BEGIN
  SELECT RAISE(ABORT, 'bulk storage full');
END;

-- Retiring pairing authority makes its responses and pending requests unusable.
-- Never compact current replay state merely because time passes or IP is down.
-- A pause/approval hold with the same key must retain counters for safe resumption.
CREATE TRIGGER fed_bulk_revoke AFTER UPDATE OF shared_secret ON fed_peer
WHEN NEW.shared_secret IS NOT OLD.shared_secret
BEGIN
  DELETE FROM fed_bulk_state WHERE peer_id=NEW.id;
END;
