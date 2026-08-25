UPDATE fed_inbox_item
SET payload_json = CASE
      WHEN stream LIKE 'board:%' THEN json_set(
        payload_json,
        '$.uid', (SELECT mesh_id FROM fed_peer WHERE id=fed_inbox_item.peer_id) || ':' || uid,
        '$.thread_uid', (SELECT mesh_id FROM fed_peer WHERE id=fed_inbox_item.peer_id) || ':' ||
          json_extract(payload_json, '$.thread_uid')
      )
      ELSE json_set(
        payload_json,
        '$.uid', (SELECT mesh_id FROM fed_peer WHERE id=fed_inbox_item.peer_id) || ':' || uid
      )
    END,
    uid = (SELECT mesh_id FROM fed_peer WHERE id=fed_inbox_item.peer_id) || ':' || uid
WHERE uid NOT LIKE '!%:%';
