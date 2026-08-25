UPDATE mail
SET reply_peer_mesh_id = (
  SELECT p.mesh_id
  FROM fed_mail_delivery d
  JOIN fed_peer p ON p.id = d.peer_id
  WHERE d.mail_id = mail.id AND d.direction = 'in'
  LIMIT 1
)
WHERE uid LIKE 'fed:%' AND reply_peer_mesh_id IS NULL;
