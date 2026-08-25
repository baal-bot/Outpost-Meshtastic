UPDATE thread
SET post_count = (SELECT COUNT(*) FROM post WHERE post.thread_id=thread.id),
    last_post_at = COALESCE(
      (SELECT MAX(created_at) FROM post WHERE post.thread_id=thread.id),
      last_post_at
    );
