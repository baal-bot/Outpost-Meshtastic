CREATE TABLE board (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  description TEXT,
  min_read_trust TEXT NOT NULL DEFAULT 'guest',
  min_post_trust TEXT NOT NULL DEFAULT 'member',
  federated INTEGER NOT NULL DEFAULT 0,
  retention_days INTEGER,
  sort_order INTEGER NOT NULL DEFAULT 100,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE thread (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  board_id INTEGER NOT NULL REFERENCES board(id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  author_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  origin_node TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_post_at INTEGER NOT NULL,
  post_count INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0,
  locked INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0,
  lat REAL,
  lon REAL
);
CREATE INDEX idx_thread_board ON thread(board_id,pinned DESC,last_post_at DESC);

CREATE TABLE post (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  thread_id INTEGER NOT NULL REFERENCES thread(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  author_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  author_label TEXT NOT NULL,
  origin_node TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  edited_at INTEGER,
  hidden INTEGER NOT NULL DEFAULT 0,
  hidden_by TEXT,
  hidden_reason TEXT,
  UNIQUE(thread_id,seq)
);
CREATE INDEX idx_post_thread ON post(thread_id,seq);

CREATE VIRTUAL TABLE post_fts USING fts5(
  body, content='post', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER post_ai AFTER INSERT ON post BEGIN
  INSERT INTO post_fts(rowid,body) VALUES(new.id,new.body);
END;
CREATE TRIGGER post_ad AFTER DELETE ON post BEGIN
  INSERT INTO post_fts(post_fts,rowid,body) VALUES('delete',old.id,old.body);
END;
CREATE TRIGGER post_au AFTER UPDATE OF body ON post BEGIN
  INSERT INTO post_fts(post_fts,rowid,body) VALUES('delete',old.id,old.body);
  INSERT INTO post_fts(rowid,body) VALUES(new.id,new.body);
END;

CREATE TABLE read_marker (
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  scope TEXT NOT NULL,
  last_seen_at INTEGER NOT NULL,
  last_seen_id INTEGER,
  PRIMARY KEY(member_id,scope)
);

CREATE TABLE subscription (
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  board_id INTEGER NOT NULL REFERENCES board(id) ON DELETE CASCADE,
  cadence TEXT NOT NULL DEFAULT 'on_request',
  created_at INTEGER NOT NULL,
  PRIMARY KEY(member_id,board_id)
);

CREATE TABLE mail (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  from_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  from_label TEXT NOT NULL,
  to_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  to_label TEXT NOT NULL,
  to_node TEXT,
  subject TEXT,
  body TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  delivered_at INTEGER,
  read_at INTEGER,
  state TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER NOT NULL,
  in_reply_to INTEGER REFERENCES mail(id) ON DELETE SET NULL
);
CREATE INDEX idx_mail_to ON mail(to_id,state,created_at DESC);
CREATE INDEX idx_mail_state ON mail(state,expires_at);

INSERT INTO board(slug,title,description,min_post_trust,sort_order,created_at) VALUES
 ('gen','General','Community discussion','member',10,unixepoch()),
 ('roads','Roads & Access','Closures and conditions','member',20,unixepoch()),
 ('lost-found','Lost & Found','Pets, gear and belongings','member',30,unixepoch()),
 ('swap','Swap & Give','Trade, lend and give away','member',40,unixepoch()),
 ('events','Events','Community events','member',50,unixepoch()),
 ('help-wanted','Help Wanted','Requests for a hand','member',60,unixepoch()),
 ('notice','Notices','Operator announcements','operator',70,unixepoch());

