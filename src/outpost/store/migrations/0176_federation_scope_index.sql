-- Bounded per-stream revision seeks, including long histories of other/private
-- streams. The page merge reads at most 101 heads from each of at most 22 streams.
CREATE INDEX idx_fed_revision_stream ON fed_revision(stream,revision,uid);
