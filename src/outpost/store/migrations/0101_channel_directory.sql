CREATE TABLE channel_dir (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  slot INTEGER,
  psk_b64 TEXT,
  published INTEGER NOT NULL DEFAULT 0,
  min_trust TEXT NOT NULL DEFAULT 'member',
  created_at INTEGER NOT NULL
);

INSERT INTO channel_dir(name,description,slot,published,min_trust,created_at) VALUES
 ('public','Primary community channel',0,1,'guest',unixepoch()),
 ('outpost','Boards and community services',2,1,'guest',unixepoch()),
 ('watch','Community watch and alerts',3,1,'guest',unixepoch());

