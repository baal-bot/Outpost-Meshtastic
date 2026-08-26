CREATE TABLE fed_peer_successor (
  old_mesh_id TEXT PRIMARY KEY,
  successor_peer_id INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  old_node_name TEXT,
  adopted_at INTEGER NOT NULL,
  adopted_by TEXT NOT NULL
);
CREATE INDEX idx_fed_peer_successor_peer ON fed_peer_successor(successor_peer_id);
