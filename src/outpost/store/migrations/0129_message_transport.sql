ALTER TABLE message_log ADD COLUMN transport TEXT;

UPDATE message_log SET transport='mesh' WHERE direction='out';

CREATE INDEX idx_msglog_federation_transport
ON message_log(airtime_class,peer_mesh_id,direction,transport,created_at DESC);
