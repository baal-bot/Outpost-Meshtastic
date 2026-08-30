ALTER TABLE fed_peer ADD COLUMN quota_mail_per_recipient_per_hour INTEGER NOT NULL DEFAULT 5
  CHECK(quota_mail_per_recipient_per_hour BETWEEN 1 AND 100);

CREATE TABLE fed_mail_usage (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  window_start INTEGER NOT NULL,
  inbound_accepted INTEGER NOT NULL DEFAULT 0,
  inbound_rejected INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(peer_id,window_start)
);

CREATE TABLE fed_mail_recipient_usage (
  peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  recipient_handle TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  inbound_accepted INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(peer_id,recipient_handle,window_start)
);

INSERT INTO fed_mail_usage(peer_id,window_start,inbound_accepted)
SELECT peer_id,created_at-created_at%3600,COUNT(*)
FROM fed_mail_delivery
WHERE direction='in'
GROUP BY peer_id,created_at-created_at%3600;

INSERT INTO fed_mail_recipient_usage(
  peer_id,recipient_handle,window_start,inbound_accepted
)
SELECT peer_id,lower(recipient_handle),created_at-created_at%3600,COUNT(*)
FROM fed_mail_delivery
WHERE direction='in'
GROUP BY peer_id,lower(recipient_handle),created_at-created_at%3600;
