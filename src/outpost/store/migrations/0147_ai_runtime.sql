CREATE TABLE kb_document (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'operator',
  pinned INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE kb_chunk (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  text TEXT NOT NULL,
  token_count INTEGER NOT NULL,
  embedding BLOB,
  UNIQUE(document_id,seq)
);

CREATE VIRTUAL TABLE kb_fts USING fts5(
  text, content='kb_chunk', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER kb_chunk_ai AFTER INSERT ON kb_chunk BEGIN
  INSERT INTO kb_fts(rowid,text) VALUES(new.id,new.text);
END;
CREATE TRIGGER kb_chunk_ad AFTER DELETE ON kb_chunk BEGIN
  INSERT INTO kb_fts(kb_fts,rowid,text) VALUES('delete',old.id,old.text);
END;
CREATE TRIGGER kb_chunk_au AFTER UPDATE OF text ON kb_chunk BEGIN
  INSERT INTO kb_fts(kb_fts,rowid,text) VALUES('delete',old.id,old.text);
  INSERT INTO kb_fts(rowid,text) VALUES(new.id,new.text);
END;

CREATE TABLE ai_interaction (
  id INTEGER PRIMARY KEY,
  member_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  channel INTEGER NOT NULL,
  question TEXT NOT NULL,
  question_class TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  tools_called TEXT NOT NULL DEFAULT '[]',
  evidence_refs TEXT NOT NULL DEFAULT '[]',
  answer TEXT,
  grounded INTEGER NOT NULL DEFAULT 0,
  refused INTEGER NOT NULL DEFAULT 0,
  refusal_reason TEXT,
  outcome TEXT NOT NULL,
  prompt_tokens INTEGER,
  output_tokens INTEGER,
  ttft_ms INTEGER,
  total_ms INTEGER,
  rated INTEGER CHECK(rated IN (-1,0,1)),
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_ai_interaction_review ON ai_interaction(rated,created_at DESC);
CREATE INDEX idx_ai_interaction_member ON ai_interaction(member_id,created_at DESC);

CREATE TABLE ai_refusal_rule (
  id INTEGER PRIMARY KEY,
  phrase TEXT NOT NULL COLLATE NOCASE UNIQUE,
  reason TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

INSERT INTO kb_document(id,slug,title,body,source,pinned,created_at,updated_at) VALUES
  (1,'using-outpost','Using Outpost','Send ? for commands. Use BOARDS to list boards, SEARCH for posts, and MAIL for private messages.','seed',1,unixepoch(),unixepoch()),
  (2,'operator-contact','Operator contact','Use MAIL operator <message> to contact the local Outpost operator.','seed',1,unixepoch(),unixepoch()),
  (3,'emergency-disclaimer','Emergency disclaimer','Outpost is not an emergency service. Call the configured emergency number if possible; use REPORT to tell local responders.','seed',1,unixepoch(),unixepoch()),
  (4,'shelter-placeholder','Shelter locations and hours','No verified shelter locations or hours are configured. Ask the Outpost operator.','seed',1,unixepoch(),unixepoch()),
  (5,'transfer-station-placeholder','Transfer station hours','No verified transfer station hours are configured. Ask the Outpost operator.','seed',1,unixepoch(),unixepoch()),
  (6,'burn-rules-placeholder','Local burn rules','No verified local burn rules are configured. Ask the Outpost operator.','seed',1,unixepoch(),unixepoch()),
  (7,'plow-placeholder','Road plow schedule','No verified road plow schedule is configured. Ask the Outpost operator.','seed',1,unixepoch(),unixepoch()),
  (8,'services-placeholder','Water food fuel and charging services','No verified potable water, food, fuel, or charging service details are configured. Ask the Outpost operator.','seed',1,unixepoch(),unixepoch());

INSERT INTO kb_chunk(document_id,seq,text,token_count)
SELECT id,1,title || ': ' || body,(length(CAST(title || ': ' || body AS BLOB))+2)/3
FROM kb_document;
