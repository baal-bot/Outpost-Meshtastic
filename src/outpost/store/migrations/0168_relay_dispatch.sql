ALTER TABLE fed_relay_envelope ADD COLUMN dispatch_status TEXT
  CHECK(dispatch_status IN ('pending','dispatched','ignored','failed'));
ALTER TABLE fed_relay_envelope ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fed_relay_envelope ADD COLUMN dispatched_at INTEGER;
ALTER TABLE fed_relay_envelope ADD COLUMN dispatch_error TEXT;
ALTER TABLE fed_relay_envelope ADD COLUMN dispatch_result_json TEXT;

CREATE INDEX idx_fed_relay_dispatch
ON fed_relay_envelope(direction,dispatch_status,updated_at);

ALTER TABLE fed_service_request ADD COLUMN relay_envelope_id TEXT;
ALTER TABLE fed_service_request ADD COLUMN relay_origin_node TEXT;
ALTER TABLE fed_service_request ADD COLUMN relay_response_envelope_id TEXT;
ALTER TABLE fed_service_request ADD COLUMN relay_response_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fed_service_request ADD COLUMN relay_response_error TEXT;
CREATE UNIQUE INDEX idx_fed_service_relay_envelope
ON fed_service_request(relay_envelope_id) WHERE relay_envelope_id IS NOT NULL;
CREATE INDEX idx_fed_service_relay_pending
ON fed_service_request(direction,relay_response_envelope_id,status,updated_at)
WHERE relay_envelope_id IS NOT NULL;

-- Permission to send a request necessarily includes permission to return its receipt.
UPDATE fed_relay_policy
SET scopes_json=json_insert(scopes_json,'$[#]','receipt')
WHERE EXISTS (SELECT 1 FROM json_each(scopes_json) WHERE value='request')
  AND NOT EXISTS (SELECT 1 FROM json_each(scopes_json) WHERE value='receipt');

-- Destination payloads accepted by older releases have verified custody but were never
-- passed to their domain. Requeue them visibly for one bounded recovery attempt at startup.
UPDATE fed_relay_envelope
SET state='rejected',dispatch_status='pending',receipt_sent_at=NULL,
    dispatch_error='Local domain dispatch is pending after upgrade',
    last_error='Local domain dispatch is pending after upgrade'
WHERE direction='destination' AND state='delivered' AND scope<>'opaque'
  AND payload_cbor IS NOT NULL;

UPDATE fed_relay_envelope
SET dispatch_status='ignored',dispatched_at=updated_at,
    dispatch_result_json='{"reason":"opaque extension payload retained without local dispatch"}'
WHERE direction='destination' AND state='delivered' AND scope='opaque';
