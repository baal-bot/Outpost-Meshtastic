ALTER TABLE fed_post_delivery ADD COLUMN wire_counter INTEGER
  CHECK(wire_counter BETWEEN 1 AND 4294967295);
