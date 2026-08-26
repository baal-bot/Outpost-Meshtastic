# 05 — Data Model

**Status:** Baseline · **Prerequisite:** [02-ARCHITECTURE.md](02-ARCHITECTURE.md)
**Implements:** `src/outpost/store/`

---

## 1. Storage decisions

**REQ-DATA-001** — Persistence **MUST** be SQLite in WAL mode, accessed through the stdlib
`sqlite3` driver in a thread executor. No ORM. Repository classes expose typed methods over
parameterised SQL.

**REQ-DATA-002** — Two distinct pragma sets, applied at different times. Conflating them is
a common and silent bug.

**(a) Database-creation pragmas — applied once, in migration `0000`, before any table
exists.** `auto_vacuum` cannot be changed later without a full `VACUUM`, and `journal_mode`
is a persistent property of the database file, not of a connection.

```sql
PRAGMA journal_mode = WAL;         -- persistent; set once, verified thereafter
PRAGMA auto_vacuum  = INCREMENTAL; -- MUST precede the first CREATE TABLE
```

**(b) Per-connection pragmas — applied on every connection opened:**

```sql
PRAGMA synchronous  = NORMAL;      -- durable enough with WAL; far kinder to SD cards
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA temp_store   = MEMORY;
PRAGMA cache_size   = -16000;      -- 16000 KiB ≈ 15.6 MiB
PRAGMA mmap_size    = 67108864;    -- 64 MB
```

**REQ-DATA-002a** — Startup **MUST** verify `PRAGMA journal_mode` returns `wal` and
`PRAGMA auto_vacuum` returns `2` (incremental), and **MUST** log an actionable error naming
this requirement if either is wrong — which indicates a database created by an older or
hand-rolled path.

**REQ-DATA-002b** — Minimum SQLite version is **3.43**, required for `contentless_delete`
on FTS5 tables (§4). Startup **MUST** check `sqlite_version()` and refuse to run below it.

**REQ-DATA-003** — There **MUST** be exactly one write connection, serialised through a
dedicated executor. Read connections **MAY** be pooled (default 3).

**REQ-DATA-004** — All timestamps **MUST** be stored as **integer Unix epoch seconds, UTC**.
No ISO strings, no local time, no `DATETIME` columns. Rendering to local time happens at the
edges only.

**REQ-DATA-005** — All IDs of user-visible entities **MUST** have both a stable internal
`id INTEGER PRIMARY KEY` and, where the entity is federated, a globally unique
`uid TEXT` of the form `<origin_node_id>:<local_id>` — because two nodes will both mint
local id 42.

---

## 2. Migrations

**REQ-DATA-006** — Migrations **MUST** be numbered SQL files in
`src/outpost/store/migrations/` named `NNNN_description.sql`, applied in order at startup
inside a transaction, with the applied version recorded in a `schema_version` table.

**REQ-DATA-007** — Migrations **MUST** be forward-only. No down-migrations. Recovery is via
backup restore.

**REQ-DATA-008** — Startup **MUST** refuse to run if the database schema version is *higher*
than the binary knows about (a downgrade), and **MUST** log clearly.

**REQ-DATA-009** — Each module owns its migration files and **MUST** number them within a
reserved band to avoid collisions when modules are developed in parallel:

| Band | Owner |
|---|---|
| 0000–0099 | core (members, sessions, message_log, config, kv) |
| 0100–0199 | bbs / mail |
| 0200–0299 | watch |
| 0300–0399 | env |
| 0400–0499 | ai |
| 0500–0599 | fed |
| 0600–0699 | web / auth |

---

## 3. Core schema

### 3.1 Members

```sql
CREATE TABLE member (
  id             INTEGER PRIMARY KEY,
  mesh_id        TEXT    NOT NULL UNIQUE,       -- "!a1b2c3d4"
  mesh_num       INTEGER NOT NULL UNIQUE,       -- 32-bit node number
  handle         TEXT    UNIQUE,                -- claimed, lowercase, 2-12 chars
  long_name      TEXT,                          -- from NODEINFO_APP
  short_name     TEXT,                          -- from NODEINFO_APP
  hw_model       TEXT,
  public_key     BLOB,                          -- Meshtastic PKI pubkey if known
  trust          TEXT    NOT NULL DEFAULT 'guest',
                 -- guest | member | trusted | responder | operator | blocked
  first_seen     INTEGER NOT NULL,
  last_seen      INTEGER NOT NULL,
  last_heard_snr REAL,
  hops_away      INTEGER,
  muted_until    INTEGER,                       -- self-requested STOP
  blocked_until  INTEGER,                       -- operator action
  unreachable_since INTEGER,                    -- set by REQ-TRANSPORT-045
  prefs          TEXT NOT NULL DEFAULT '{}',
       -- JSON: {"digest_cadence","digest_hour","units","position":"full|coarse|off",
       --        "mail_notify":"piggyback|immediate","quiet_hours"}
  notes          TEXT,                          -- operator-only
  handle_changed_at INTEGER,
  prior_handle   TEXT,                          -- reserved 30d against impersonation
  prior_handle_until INTEGER
);
CREATE INDEX idx_member_last_seen ON member(last_seen DESC);
CREATE INDEX idx_member_handle    ON member(handle);
```

**REQ-DATA-010** — A `member` row **MUST** be created on first contact with trust `guest`.
Creating a member is not registration; claiming a handle is.

**REQ-DATA-011** — `mesh_id` is the identity key. Handles are labels and **MUST NOT** be
used as a foreign key anywhere.

### 3.2 Positions

```sql
CREATE TABLE member_position (
  member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  reported_at INTEGER NOT NULL,
  lat         REAL    NOT NULL,
  lon         REAL    NOT NULL,
  alt_m       REAL,
  precision_bits INTEGER,                       -- as reported by Meshtastic
  source      TEXT NOT NULL,                    -- position_app | manual | incident
  PRIMARY KEY (member_id, reported_at)
);
CREATE INDEX idx_pos_time ON member_position(reported_at DESC);

CREATE TABLE member_position_current (
  member_id   INTEGER PRIMARY KEY REFERENCES member(id) ON DELETE CASCADE,
  reported_at INTEGER NOT NULL,
  lat REAL NOT NULL, lon REAL NOT NULL, alt_m REAL,
  precision_bits INTEGER
);
```

**REQ-DATA-012** — Position history **MUST** be retained no longer than
`store.retention.position_days` (default **7**) and **MUST** be prunable to zero by operator
config. The `_current` table is the only long-lived position record. See doc 12 §8.

**REQ-DATA-013** — Position history **MUST NOT** be exposed through any over-the-air command.
`LAST <handle>` returns only the current position, and only at the precision the sender
chose, and only if that member has not disabled position sharing.

### 3.3 Message log

```sql
CREATE TABLE message_log (
  id           INTEGER PRIMARY KEY,
  direction    TEXT    NOT NULL,          -- in | out
  member_id    INTEGER REFERENCES member(id) ON DELETE SET NULL,
  peer_mesh_id TEXT,                      -- preserved even if member deleted
  channel      INTEGER NOT NULL,
  portnum      INTEGER NOT NULL,
  is_direct    INTEGER NOT NULL,
  packet_id    INTEGER,
  text         TEXT,
  byte_len     INTEGER NOT NULL,
  toa_ms       INTEGER,                   -- estimated time on air
  airtime_class TEXT,                     -- out only
  command      TEXT,                      -- resolved command name, in only
  outcome      TEXT,                      -- acked|naked|timeout|not_requested|dropped
  drop_reason  TEXT,
  latency_ms   INTEGER,                   -- in→out for replies
  rx_snr REAL, rx_rssi INTEGER, hops INTEGER,
  created_at   INTEGER NOT NULL
);
CREATE INDEX idx_msglog_time    ON message_log(created_at DESC);
CREATE INDEX idx_msglog_member  ON message_log(member_id, created_at DESC);
CREATE INDEX idx_msglog_command ON message_log(command, created_at DESC);
```

**REQ-DATA-014** — Every inbound and every outbound message **MUST** produce exactly one
`message_log` row, including dropped and throttled outbound items. This table is the audit
trail and the source of the airtime analytics on the dashboard.

**REQ-DATA-015** — `message_log` **MUST** be pruned on the retention schedule (default 30
days) and **MUST NOT** be allowed to dominate database size. A row-count ceiling
(default 500 000) triggers pruning regardless of age.

### 3.4 Key-value and config

```sql
CREATE TABLE kv (
  ns         TEXT NOT NULL,
  k          TEXT NOT NULL,
  v          TEXT NOT NULL,          -- JSON
  expires_at INTEGER,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (ns, k)
);

CREATE TABLE audit_log (
  id         INTEGER PRIMARY KEY,
  actor_kind TEXT NOT NULL,          -- member | web_user | system
  actor_ref  TEXT NOT NULL,
  action     TEXT NOT NULL,
  target     TEXT,
  detail     TEXT,                   -- JSON
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_audit_time ON audit_log(created_at DESC);
```

**REQ-DATA-016** — Every privileged action (trust change, post removal, mute, alert raise,
config change, federation peer add) **MUST** write an `audit_log` row. Audit rows are
**never** pruned automatically.

---

## 4. BBS schema

```sql
CREATE TABLE board (
  id          INTEGER PRIMARY KEY,
  slug        TEXT NOT NULL UNIQUE,           -- lowercase, [a-z0-9-], ≤16 chars
  title       TEXT NOT NULL,                  -- ≤40 chars
  description TEXT,
  min_read_trust  TEXT NOT NULL DEFAULT 'guest',
  min_post_trust  TEXT NOT NULL DEFAULT 'member',
  federated   INTEGER NOT NULL DEFAULT 0,
  retention_days INTEGER,                     -- NULL = use global default
  sort_order  INTEGER NOT NULL DEFAULT 100,
  archived    INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL
);

CREATE TABLE thread (
  id          INTEGER PRIMARY KEY,
  uid         TEXT NOT NULL UNIQUE,           -- "<origin>:<local>"
  board_id    INTEGER NOT NULL REFERENCES board(id) ON DELETE CASCADE,
  subject     TEXT NOT NULL,                  -- ≤64 chars, derived if not given
  author_id   INTEGER REFERENCES member(id) ON DELETE SET NULL,
  origin_node TEXT NOT NULL,                  -- mesh id of the node that minted it
  created_at  INTEGER NOT NULL,
  last_post_at INTEGER NOT NULL,
  post_count  INTEGER NOT NULL DEFAULT 0,
  pinned      INTEGER NOT NULL DEFAULT 0,
  locked      INTEGER NOT NULL DEFAULT 0,
  hidden      INTEGER NOT NULL DEFAULT 0,     -- moderated out; soft delete
  lat REAL, lon REAL                          -- optional geotag
);
CREATE INDEX idx_thread_board ON thread(board_id, pinned DESC, last_post_at DESC);

CREATE TABLE post (
  id          INTEGER PRIMARY KEY,
  uid         TEXT NOT NULL UNIQUE,
  thread_id   INTEGER NOT NULL REFERENCES thread(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,               -- 1-based within thread
  author_id   INTEGER REFERENCES member(id) ON DELETE SET NULL,
  author_label TEXT NOT NULL,                 -- denormalised handle at time of posting
  origin_node TEXT NOT NULL,
  body        TEXT NOT NULL,                  -- ≤1000 chars; over-air posts ≤200
  created_at  INTEGER NOT NULL,
  edited_at   INTEGER,
  hidden      INTEGER NOT NULL DEFAULT 0,
  hidden_by   TEXT,
  hidden_reason TEXT,
  UNIQUE (thread_id, seq)
);
CREATE INDEX idx_post_thread ON post(thread_id, seq);

-- External-content FTS: rowid maps 1:1 to post.id, so DELETE and UPDATE work,
-- snippet()/highlight() return real text, and `rebuild` is supported.
CREATE VIRTUAL TABLE post_fts USING fts5(
  body, content='post', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER post_ai AFTER INSERT ON post BEGIN
  INSERT INTO post_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER post_ad AFTER DELETE ON post BEGIN
  INSERT INTO post_fts(post_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER post_au AFTER UPDATE OF body ON post BEGIN
  INSERT INTO post_fts(post_fts, rowid, body) VALUES ('delete', old.id, old.body);
  INSERT INTO post_fts(rowid, body) VALUES (new.id, new.body);
END;

-- Subjects live on `thread`, not `post`, so they get their own index.
CREATE VIRTUAL TABLE thread_fts USING fts5(
  subject, content='thread', content_rowid='id', tokenize='porter unicode61'
);
-- (equivalent ai/ad/au triggers on thread.subject)

CREATE TABLE subscription (
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  board_id  INTEGER NOT NULL REFERENCES board(id) ON DELETE CASCADE,
  cadence   TEXT NOT NULL DEFAULT 'on_request',  -- on_request | daily | immediate
  created_at INTEGER NOT NULL,
  PRIMARY KEY (member_id, board_id)
);

CREATE TABLE read_marker (
  member_id  INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  scope      TEXT NOT NULL,          -- 'board:<id>' | 'thread:<id>' | 'mail'
  last_seen_at INTEGER NOT NULL,
  last_seen_id INTEGER,
  PRIMARY KEY (member_id, scope)
);

-- Partial compositions saved when a pending action times out (doc 04 §6)
CREATE TABLE draft (
  id         INTEGER PRIMARY KEY,
  member_id  INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,          -- post | reply | mail | incident
  target     TEXT,                   -- board slug, thread uid, handle, incident ref
  body       TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  notified   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_draft_member ON draft(member_id, created_at DESC);

-- Community channel directory (doc 07 §8)
CREATE TABLE channel_dir (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,  -- ≤12 bytes, Meshtastic channel name
  description TEXT,
  slot        INTEGER,               -- 0-7 if this node carries it
  psk_b64     TEXT,                  -- NULL unless the operator published it
  published   INTEGER NOT NULL DEFAULT 0,
  min_trust   TEXT NOT NULL DEFAULT 'member',
  created_at  INTEGER NOT NULL
);
```

**REQ-DATA-019a** — `channel_dir.psk_b64` is a **secret at rest**. It **MUST NOT** appear in
logs (REQ-SEC-037), **MUST NOT** be returned by any unauthenticated API, **MUST NOT** be an
AI retrieval source, and **MUST** be included in the encrypted-backup scope (REQ-SEC-042).
A channel with `published = 0` **MUST** never have its PSK transmitted, on any path.

**REQ-DATA-017** — Moderation **MUST** be a soft delete (`hidden = 1`) with actor and
reason. Hard deletion is only performed by the retention pruner.

**REQ-DATA-018** — `post_fts` and `thread_fts` are **external-content** FTS5 tables kept in
sync by the triggers above. `hidden` is **not** an FTS column — hiding a post does not
touch the index; the search query joins to `post`/`thread`/`board` and filters
`hidden = 0` plus the requester's read permissions there. Indexing a mutable visibility
flag into FTS is the wrong shape and would require reindexing on every moderation action.

**REQ-DATA-019** — `author_label` **MUST** be denormalised at write time so a rendered post
does not require a join and so a later handle change does not rewrite history.

---

## 5. Mail schema

```sql
CREATE TABLE mail (
  id          INTEGER PRIMARY KEY,
  uid         TEXT NOT NULL UNIQUE,
  from_id     INTEGER REFERENCES member(id) ON DELETE SET NULL,
  from_label  TEXT NOT NULL,
  to_id       INTEGER REFERENCES member(id) ON DELETE SET NULL,
  to_label    TEXT NOT NULL,                  -- may be an unresolved handle
  to_node     TEXT,                           -- destination Outpost node for federated mail
  subject     TEXT,
  body        TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  delivered_at INTEGER,
  read_at     INTEGER,
  state       TEXT NOT NULL DEFAULT 'queued',
              -- queued | notified | read | expired | undeliverable | forwarded
  attempts    INTEGER NOT NULL DEFAULT 0,
  expires_at  INTEGER NOT NULL,
  in_reply_to INTEGER REFERENCES mail(id) ON DELETE SET NULL
);
CREATE INDEX idx_mail_to    ON mail(to_id, state, created_at DESC);
CREATE INDEX idx_mail_state ON mail(state, expires_at);
```

**REQ-DATA-020** — Mail is **store-and-notify**, not store-and-push. The node **MUST NOT**
transmit the mail body unsolicited; it notifies the recipient that mail is waiting (piggy-
backed on their next interaction where possible) and transmits the body on `READMAIL`.

**REQ-DATA-021** — Mail bodies are **not** end-to-end encrypted and this **MUST** be stated
in `HELP MAIL` and on the dashboard. The node operator can read all mail. Meshtastic PKI
protects the radio hop; the node itself is a plaintext store. See doc 12 §7.

---

## 6. Community watch schema

```sql
CREATE TABLE incident (
  id          INTEGER PRIMARY KEY,
  uid         TEXT NOT NULL UNIQUE,
  local_ref   INTEGER NOT NULL,               -- short number shown to users
  type        TEXT NOT NULL,                  -- see doc 08 §2
  severity    TEXT NOT NULL,                  -- info | caution | urgent | critical
  status      TEXT NOT NULL DEFAULT 'open',   -- open | monitoring | resolved | false_alarm | expired
  title       TEXT NOT NULL,                  -- ≤64
  body        TEXT,
  lat REAL, lon REAL, location_text TEXT,
  radius_m    INTEGER,
  reporter_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  reporter_label TEXT NOT NULL,
  origin_node TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  expires_at  INTEGER,
  resolved_at INTEGER,
  resolved_by TEXT,
  resolution_note TEXT,
  confirm_count INTEGER NOT NULL DEFAULT 0,
  dispute_count INTEGER NOT NULL DEFAULT 0,
  source      TEXT NOT NULL DEFAULT 'member', -- member | operator | cap | same | federated
  location_unconfirmed INTEGER NOT NULL DEFAULT 0,  -- free-text location, no lat/lon
  position_suppressed  INTEGER NOT NULL DEFAULT 0,  -- reporter used -nopos
  unverified  INTEGER NOT NULL DEFAULT 0,     -- filed by a `guest` (REQ-SEC-008)
  flagged_for_review INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_incident_status ON incident(status, severity, updated_at DESC);
CREATE INDEX idx_incident_geo    ON incident(lat, lon);

CREATE TABLE incident_update (
  id          INTEGER PRIMARY KEY,
  uid         TEXT NOT NULL UNIQUE,
  incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  author_id   INTEGER REFERENCES member(id) ON DELETE SET NULL,
  author_label TEXT NOT NULL,
  kind        TEXT NOT NULL,        -- update | confirm | dispute | ack | status_change
  body        TEXT,
  lat REAL, lon REAL,
  created_at  INTEGER NOT NULL,
  UNIQUE (incident_id, seq)
);

CREATE TABLE alert (
  id          INTEGER PRIMARY KEY,
  uid         TEXT NOT NULL UNIQUE,
  incident_id INTEGER REFERENCES incident(id) ON DELETE SET NULL,
  severity    TEXT NOT NULL,        -- caution | urgent | critical
  headline    TEXT NOT NULL,        -- ≤140 bytes: this is what goes on the air
  body        TEXT,
  source      TEXT NOT NULL,        -- operator | incident | cap | same
  source_ref  TEXT,                 -- CAP identifier / SAME header
  channels    TEXT NOT NULL,        -- JSON array of channel indices
  raised_by   TEXT NOT NULL,
  raised_at   INTEGER NOT NULL,
  effective_at INTEGER,
  expires_at  INTEGER,
  cancelled_at INTEGER,
  escalation_stage INTEGER NOT NULL DEFAULT 0,
  next_escalation_at INTEGER,
  ack_required INTEGER NOT NULL DEFAULT 0,
  broadcast_count INTEGER NOT NULL DEFAULT 0,
  repeat_count INTEGER NOT NULL DEFAULT 0,
  all_clear_at INTEGER,
  all_clear_queued INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_alert_active ON alert(expires_at, cancelled_at);

CREATE TABLE alert_ack (
  alert_id  INTEGER NOT NULL REFERENCES alert(id) ON DELETE CASCADE,
  member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  acked_at  INTEGER NOT NULL,
  note      TEXT,
  PRIMARY KEY (alert_id, member_id)
);

CREATE TABLE checkin (
  id         INTEGER PRIMARY KEY,
  member_id  INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
  event_id   INTEGER REFERENCES watch_event(id) ON DELETE SET NULL,
  status     TEXT NOT NULL,        -- ok | need_help | unaccounted | evacuated
  note       TEXT,
  lat REAL, lon REAL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_checkin_member ON checkin(member_id, created_at DESC);

CREATE TABLE watch_event (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  opened_at  INTEGER NOT NULL,
  closed_at  INTEGER,
  opened_by  TEXT NOT NULL,
  roster_policy TEXT NOT NULL DEFAULT 'all'  -- all | responders | subscribed
);
```

`alert_audience` records each distinct destination/channel admitted for an alert so an all-clear
uses the same audience footprint rather than only the first configured channel.

**REQ-DATA-022** — `local_ref` **MUST** be a small integer, unique among *currently visible*
incidents, recycled only after the incident leaves the active window. Users type `I 31`, not
`I 4f3a-9b21-...`.

**REQ-DATA-023** — `alert.headline` **MUST** be validated at ≤140 bytes on write, so the
rendered broadcast (headline + severity marker + expiry + node prefix) fits one packet.

---

## 7. Environment schema

```sql
CREATE TABLE wx_cache (
  id          INTEGER PRIMARY KEY,
  provider    TEXT NOT NULL,        -- nws | open_meteo
  kind        TEXT NOT NULL,        -- points | forecast | hourly | current | obs
  key         TEXT NOT NULL,        -- normalised location or grid key
  payload     TEXT NOT NULL,        -- JSON
  source_updated_at INTEGER,        -- provider's own updateTime
  fetched_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  etag        TEXT,
  last_modified TEXT,
  UNIQUE (provider, kind, key)
);

CREATE TABLE cap_alert (
  id          INTEGER PRIMARY KEY,
  identifier  TEXT NOT NULL UNIQUE, -- CAP identifier
  sender      TEXT, sent INTEGER,
  status TEXT, msg_type TEXT, category TEXT,
  event TEXT NOT NULL,
  urgency TEXT, severity TEXT, certainty TEXT,
  headline TEXT, description TEXT, instruction TEXT,
  area_desc TEXT,
  geocodes    TEXT,                 -- JSON: {"SAME":[...],"UGC":[...]}
  polygon     TEXT,                 -- JSON coordinate array
  effective INTEGER, onset INTEGER, expires INTEGER, ends INTEGER,
  ingested_at INTEGER NOT NULL,
  ingest_source TEXT NOT NULL,      -- nws_api | same_radio | federated
  broadcast_at INTEGER,
  superseded_by TEXT
);
CREATE INDEX idx_cap_active ON cap_alert(expires, severity);

CREATE TABLE cap_point_cache (
  cache_key TEXT PRIMARY KEY,       -- provider/service + normalised lat/lon
  provider TEXT NOT NULL,
  query_lat REAL NOT NULL, query_lon REAL NOT NULL,
  service_area TEXT,
  status TEXT NOT NULL,             -- ok | empty | unsupported_region
  result_json TEXT NOT NULL,
  provider_timestamp TEXT,
  fetched_at INTEGER NOT NULL
);

CREATE TABLE waypoint (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  lat REAL NOT NULL, lon REAL NOT NULL,
  kind       TEXT,                  -- shelter | water | fuel | hazard | trailhead | other
  created_by INTEGER REFERENCES member(id) ON DELETE SET NULL,
  public     INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
```

**REQ-DATA-024** — Cached weather **MUST** carry the provider's own `updateTime` separately
from the node's `fetched_at`, because staleness is measured from when the *forecast* was
issued, not from when we happened to download it.

**REQ-DATA-025** — A CAP alert **MUST** be deduplicated across ingest sources by
`identifier` where available, and by `(event, geocode, expires)` for SAME-radio ingest which
carries no CAP identifier.

---

## 8. AI schema

```sql
CREATE TABLE kb_document (
  id         INTEGER PRIMARY KEY,
  title      TEXT NOT NULL,
  body       TEXT NOT NULL,
  source     TEXT NOT NULL,        -- operator | board | incident | wx | file
  source_ref TEXT,
  tags       TEXT,                 -- JSON array
  pinned     INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE kb_chunk (
  id          INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  text        TEXT NOT NULL,
  token_count INTEGER NOT NULL,
  embedding   BLOB,                -- float32 array; NULL if embeddings disabled
  UNIQUE (document_id, seq)
);

CREATE VIRTUAL TABLE kb_fts USING fts5(
  text, content='kb_chunk', content_rowid='id', tokenize='porter unicode61'
);
-- ai/ad/au triggers on kb_chunk.text, as for post_fts

CREATE TABLE ai_interaction (
  id            INTEGER PRIMARY KEY,
  member_id     INTEGER REFERENCES member(id) ON DELETE SET NULL,
  channel       INTEGER NOT NULL,
  question      TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  tools_called  TEXT,              -- JSON array
  evidence_refs TEXT,              -- JSON array of source refs
  answer        TEXT,
  grounded      INTEGER NOT NULL DEFAULT 0,
  refused       INTEGER NOT NULL DEFAULT 0,
  refusal_reason TEXT,
  prompt_tokens INTEGER, output_tokens INTEGER,
  ttft_ms INTEGER, total_ms INTEGER,
  rated         INTEGER,           -- operator rating -1/0/1
  created_at    INTEGER NOT NULL
);
```

**REQ-DATA-026** — Every AI interaction **MUST** be logged with its evidence references, so
the operator can audit what the assistant told the community and why. This is a safety
requirement.

**REQ-DATA-027** — Embeddings **MUST** be optional. When the embedding model is unavailable
or disabled, retrieval falls back to FTS5 BM25 alone and the system **MUST** remain fully
functional. See doc 06 §4.3.

---

## 9. Federation schema

```sql
CREATE TABLE fed_peer (
  id          INTEGER PRIMARY KEY,
  mesh_id     TEXT NOT NULL UNIQUE,
  node_name   TEXT,
  state       TEXT NOT NULL DEFAULT 'pending',  -- pending|active|paused|rejected
  protocol_version INTEGER,
  shared_secret BLOB,                           -- 32 random bytes; SECRET AT REST
  tx_counter  INTEGER NOT NULL DEFAULT 0,       -- our next outbound frame counter
  rx_counter  INTEGER NOT NULL DEFAULT 0,       -- highest accepted inbound counter
  boards      TEXT NOT NULL DEFAULT '[]',
       -- JSON array of {slug, direction}, direction ∈ send|recv|both  (REQ-FED-016)
  sync_incidents INTEGER NOT NULL DEFAULT 0,
  relay_mail  INTEGER NOT NULL DEFAULT 0,
  relay_alerts INTEGER NOT NULL DEFAULT 0,      -- REQ-FED-035, default off
  auto_accept_alerts TEXT NOT NULL DEFAULT '[]',-- JSON array of severities  (REQ-FED-036)
  quota_items_per_hour INTEGER NOT NULL DEFAULT 200,
  quota_mail_per_hour  INTEGER NOT NULL DEFAULT 20,
  last_sync_at INTEGER,
  last_seen_at INTEGER,
  paused_reason TEXT,
  approved_by TEXT, approved_at INTEGER,
  created_at  INTEGER NOT NULL
);

-- Loop prevention: what we have already sent to / received from each peer (REQ-FED-027)
CREATE TABLE fed_seen (
  peer_id    INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  uid        TEXT    NOT NULL,
  direction  TEXT    NOT NULL,        -- send | recv
  seen_at    INTEGER NOT NULL,
  PRIMARY KEY (peer_id, uid, direction)
);
CREATE INDEX idx_fed_seen_age ON fed_seen(seen_at);

CREATE TABLE fed_cursor (
  peer_id    INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  stream     TEXT NOT NULL,        -- 'board:<slug>' | 'incidents' | 'mail'
  direction  TEXT NOT NULL,        -- send | recv
  cursor     TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (peer_id, stream, direction)
);

CREATE TABLE fed_outbox (
  id         INTEGER PRIMARY KEY,
  peer_id    INTEGER NOT NULL REFERENCES fed_peer(id) ON DELETE CASCADE,
  msg_type   INTEGER NOT NULL,
  body       BLOB NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  sent_at    INTEGER
);
```

---

## 10. Retention and maintenance

**REQ-DATA-028** — A maintenance task **MUST** run on a schedule (default 03:00 local) and:

1. Prune per the retention config (posts, mail, incidents, positions, message_log, wx_cache,
   ai_interaction).
2. Delete expired `kv` rows.
3. Run `INSERT INTO post_fts(post_fts) VALUES('optimize')` (and the same for `thread_fts`,
   `kb_fts`) — the supported maintenance operation on external-content FTS5 tables.
4. Prune `fed_seen` rows older than `fed.peer_stale_hours × 2`.
5. Run `PRAGMA optimize`.
6. Run `PRAGMA incremental_vacuum` — effective only because `auto_vacuum = INCREMENTAL`
   was set in migration `0000` (REQ-DATA-002a).
7. Emit a database-size metric.

**REQ-DATA-029** — Retention pruning **MUST NOT** delete: pinned threads, unresolved
incidents, alerts, audit_log rows, operator-authored KB documents, or member rows with a
handle.

**REQ-DATA-030** — Backups **MUST** use `sqlite3.Connection.backup()` (the online backup
API), never a file copy, and **MUST** be verified with `PRAGMA integrity_check` on the copy
before rotating out the oldest.

**REQ-DATA-031** — The target database size on the reference hardware **MUST** stay under
**500 MB** at default retention with an active community; if projected to exceed it, the
pruner tightens retention automatically and warns the operator.

---

## 11. Repository interface convention

**REQ-DATA-032** — Repositories **MUST** expose domain-typed methods returning frozen
dataclasses — never raw rows, dicts, or `sqlite3.Row` — so that a change of storage engine
is a contained refactor:

```python
class ThreadRepo:
    async def create(self, *, board_id: int, subject: str, author: Member,
                     body: str, geo: LatLon | None) -> Thread: ...
    async def list_recent(self, board_id: int, *, limit: int,
                          before: int | None, include_hidden: bool = False
                          ) -> list[ThreadSummary]: ...
    async def get(self, thread_id: int) -> Thread | None: ...
    async def unread_count(self, board_id: int, member_id: int) -> int: ...
```

**REQ-DATA-033** — Every repository method that accepts a member **MUST** take an already-
authorised caller; repositories **MUST NOT** perform authorisation themselves. Authorisation
lives in the domain services (doc 12 §4).

**REQ-DATA-034** — All SQL **MUST** be parameterised. String interpolation into SQL is a
build-failing lint violation, with no exceptions for "internal" values.
