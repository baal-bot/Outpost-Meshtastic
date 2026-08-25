CREATE TEMP TABLE home_fed_thread_map AS
SELECT old_t.id AS old_id, new_t.id AS new_id
FROM thread old_t
JOIN thread new_t ON new_t.uid=substr(old_t.uid, instr(old_t.uid, ':') + 1)
WHERE old_t.uid LIKE '!%:%'
  AND NOT EXISTS (
    SELECT 1 FROM fed_peer p WHERE old_t.uid LIKE p.mesh_id || ':%'
  );

UPDATE post
SET seq = (
      SELECT COALESCE(MAX(target.seq),0) FROM post target
      WHERE target.thread_id=(
        SELECT new_id FROM home_fed_thread_map WHERE old_id=post.thread_id
      )
    ) + (
      SELECT COUNT(*) FROM post ranked
      WHERE ranked.thread_id=post.thread_id AND ranked.id<=post.id
    ),
    thread_id = (
      SELECT new_id FROM home_fed_thread_map WHERE old_id=post.thread_id
    )
WHERE thread_id IN (SELECT old_id FROM home_fed_thread_map);

DELETE FROM thread WHERE id IN (SELECT old_id FROM home_fed_thread_map);

UPDATE thread
SET post_count=(SELECT COUNT(*) FROM post WHERE post.thread_id=thread.id),
    last_post_at=COALESCE(
      (SELECT MAX(created_at) FROM post WHERE post.thread_id=thread.id),
      last_post_at
    )
WHERE id IN (SELECT new_id FROM home_fed_thread_map);

DROP TABLE home_fed_thread_map;
