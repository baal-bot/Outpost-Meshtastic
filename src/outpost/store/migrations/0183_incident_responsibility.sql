-- Local coordination only; federation incident imports never write these tables.
-- Non-reusable target identities prevent a removed member/group/account rebinding an offer.
CREATE TABLE incident_responsibility_target (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('member','group','account')),
  member_id INTEGER UNIQUE REFERENCES member(id) ON DELETE SET NULL,
  group_id INTEGER UNIQUE REFERENCES responder_group(id) ON DELETE SET NULL,
  account_id INTEGER UNIQUE REFERENCES web_account(id) ON DELETE SET NULL,
  CHECK((kind='member' AND group_id IS NULL AND account_id IS NULL)
     OR (kind='group' AND member_id IS NULL AND account_id IS NULL)
     OR (kind='account' AND member_id IS NULL AND group_id IS NULL))
);

INSERT INTO incident_responsibility_target(kind,member_id) SELECT 'member',id FROM member;
INSERT INTO incident_responsibility_target(kind,group_id) SELECT 'group',id FROM responder_group;
INSERT INTO incident_responsibility_target(kind,account_id) SELECT 'account',id FROM web_account;
CREATE TRIGGER responsibility_member_identity AFTER INSERT ON member BEGIN
  INSERT INTO incident_responsibility_target(kind,member_id) VALUES('member',NEW.id);
END;
CREATE TRIGGER responsibility_group_identity AFTER INSERT ON responder_group BEGIN
  INSERT INTO incident_responsibility_target(kind,group_id) VALUES('group',NEW.id);
END;
CREATE TRIGGER responsibility_account_identity AFTER INSERT ON web_account BEGIN
  INSERT INTO incident_responsibility_target(kind,account_id) VALUES('account',NEW.id);
END;

CREATE TABLE incident_responsibility (
  incident_id INTEGER PRIMARY KEY REFERENCES incident(id) ON DELETE CASCADE,
  version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
  owner_id INTEGER REFERENCES incident_responsibility_target(id),
  offer_id INTEGER REFERENCES incident_responsibility_target(id),
  accepted_at INTEGER,
  verified_at INTEGER,
  verification_epoch TEXT,
  next_action TEXT NOT NULL DEFAULT '' CHECK(length(CAST(next_action AS BLOB)) <= 160),
  offer_action TEXT NOT NULL DEFAULT '' CHECK(length(CAST(offer_action AS BLOB)) <= 160),
  closed_state TEXT NOT NULL DEFAULT 'unassigned'
    CHECK(closed_state IN ('unassigned','released','completed')),
  updated_at INTEGER NOT NULL
);

CREATE TABLE incident_responsibility_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('offer','accept','update','cancel','release','complete')),
  actor_member_id INTEGER REFERENCES member(id) ON DELETE SET NULL,
  actor_account_id INTEGER REFERENCES web_account(id) ON DELETE SET NULL,
  target_id INTEGER REFERENCES incident_responsibility_target(id),
  owner_id INTEGER REFERENCES incident_responsibility_target(id),
  next_action TEXT NOT NULL CHECK(length(CAST(next_action AS BLOB)) <= 160),
  created_at INTEGER NOT NULL,
  UNIQUE(incident_id,version)
);
CREATE INDEX idx_incident_responsibility_event_member
  ON incident_responsibility_event(actor_member_id);
