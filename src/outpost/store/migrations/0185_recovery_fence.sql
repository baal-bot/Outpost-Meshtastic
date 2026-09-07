-- A restored identity is evidence, not permission to run a second transmitter.
CREATE TABLE recovery_fence (
  id INTEGER PRIMARY KEY CHECK(id=1),
  bundle_digest TEXT NOT NULL CHECK(length(bundle_digest)=64),
  restored_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state='review_required')
);
