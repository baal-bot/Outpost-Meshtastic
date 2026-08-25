CREATE TEMP TABLE legacy_fed_thread_map AS
SELECT DISTINCT
  substr(i.uid, length(p.mesh_id) + 2) AS old_uid,
  json_extract(i.payload_json, '$.thread_uid') AS new_uid
FROM fed_inbox_item i
JOIN fed_peer p ON p.id=i.peer_id
WHERE i.stream LIKE 'board:%'
  AND i.uid LIKE p.mesh_id || ':%';

UPDATE post
SET uid = (
  SELECT m.new_uid FROM legacy_fed_thread_map m
  JOIN thread old_t ON old_t.uid=m.old_uid
  WHERE old_t.id=post.thread_id AND post.uid=m.old_uid
)
WHERE EXISTS (
  SELECT 1 FROM legacy_fed_thread_map m
  JOIN thread old_t ON old_t.uid=m.old_uid
  WHERE old_t.id=post.thread_id AND post.uid=m.old_uid
    AND NOT EXISTS (SELECT 1 FROM thread new_t WHERE new_t.uid=m.new_uid)
);

UPDATE thread
SET uid = (SELECT new_uid FROM legacy_fed_thread_map WHERE old_uid=thread.uid)
WHERE uid IN (SELECT old_uid FROM legacy_fed_thread_map)
  AND NOT EXISTS (
    SELECT 1 FROM legacy_fed_thread_map m
    JOIN thread new_t ON new_t.uid=m.new_uid
    WHERE m.old_uid=thread.uid
  );

UPDATE post
SET thread_id = (
  SELECT new_t.id FROM thread old_t
  JOIN legacy_fed_thread_map m ON m.old_uid=old_t.uid
  JOIN thread new_t ON new_t.uid=m.new_uid
  WHERE old_t.id=post.thread_id
)
WHERE EXISTS (
  SELECT 1 FROM thread old_t
  JOIN legacy_fed_thread_map m ON m.old_uid=old_t.uid
  JOIN thread new_t ON new_t.uid=m.new_uid
  WHERE old_t.id=post.thread_id AND post.uid<>m.old_uid
);

DELETE FROM post
WHERE EXISTS (
  SELECT 1 FROM thread old_t
  JOIN legacy_fed_thread_map m ON m.old_uid=old_t.uid
  JOIN thread new_t ON new_t.uid=m.new_uid
  WHERE old_t.id=post.thread_id AND post.uid=m.old_uid
);

DELETE FROM thread
WHERE uid IN (SELECT old_uid FROM legacy_fed_thread_map)
  AND EXISTS (
    SELECT 1 FROM legacy_fed_thread_map m
    JOIN thread new_t ON new_t.uid=m.new_uid
    WHERE m.old_uid=thread.uid
  );

DROP TABLE legacy_fed_thread_map;
