# Signed physical-transfer federation

Implements the software path for original REQ-FED-041 / [#143](https://github.com/baal-bot/Outpost-Meshtastic/issues/143).
This is selective community replication, not a database backup, encrypted mail container,
node-adoption tool, or physical-media qualification.

## Operator workflow

Open **Federation → Signed physical transfer**, or `/bundles.html`, using a named operator
account. The existing session, CSRF, module and transport-access rules apply; optional-auth
application construction does not enable an anonymous back door.

1. Before an outage, initialize federation normally, observe the local radio identity, and
   explicitly commission that exact lowercase `!1234abcd` ID. Commissioning stores it in
   migration 184. It cannot infer a local ID from a peer or replace an existing binding.
   Subsequent radio-off restarts restore that binding into the existing federation owners.
   A different observed radio identity blocks transfer pending separate node-adoption work.
2. Pair the two nodes and verify their relay-origin signing fingerprints through the existing
   origin-key review workflow. Offline transfer requires a currently active, mutually approved
   pairing and an already trusted signing pin. Discovery, a file, a self-signature, or a
   sender-selected timestamp never grants trust. First-time uncommissioned/unpaired stations
   need this preparation; the file importer is not an alternative provisioning channel.
3. Select the paired destination and one permitted stream: `board:SLUG`, `incidents`, `alerts`,
   or `incident_updates`. Preview a page of up to eight eligible original-producer records.
   Start with cursor zero; each preview reports the next cursor, completion and excluded count.
   A page scans at most 100 candidate heads. No unbounded all-history export is offered.
4. Review every field, including free text and node provenance. Public labels and structured
   precise locations are excluded by default. Explicitly enable only the needed scope, preview
   again, then separately approve public content before **Sign and download reviewed page**.
   Each file is a page, not a snapshot of the whole database. Save every desired page; signing
   or downloading is not proof the browser saved it, the carrier delivered it, or a peer imported it.
5. Carry the `.opb` file on protected media or a trusted local LAN. It is **signed, not encrypted**.
   Anyone with the file can read its content, labels, locations, node IDs and advisory times.
   Do not place it on public file shares or treat a signature as confidentiality.
6. On the destination, choose the file and **Verify and preview import**. The source does not
   need to be available; no internet or functioning radio is required. Preview writes no inbox,
   domain record, receipt, trust pin, or audit. Read all payloads and proposed effects. Explicitly
   approve public content plus each declared optional privacy scope, then **Commit reviewed import**.
7. Import parent incidents before their plain notes. A missing/unapproved parent fails the entire
   note page. Review/import the parent first and preview the note file again. A mixed externally
   generated page imports parent incidents before notes; no record is partially committed.

If state changes, refresh the preview and review again. The UI never automatically retries an
approval. If the response is lost, preview the same file: a committed file is reported as already
committed. Do not clear receipts or trust pins to force a retry. A different file with identical
already stored revisions makes no duplicate domain action.

## Privacy and exact revision identity

The supported payload schemas are the existing public federation board, incident, alert and plain
note exports. Private mail, welfare/check-ins, member/account registries, credentials, signing and
pairing secrets, and local incident responsibility are not supported streams or extra fields.
Bundles contain no actor session or operator password. Public node IDs are required provenance;
existing author/reporter/raiser labels require the public-label scope.

The precise-location flag controls structured latitude/longitude and location-description fields.
A radius alone without a location does not identify a point. Free text, subjects, labels and source
references can also contain names, addresses or coordinates: this is not an automatic content
classifier. Explicit public-content review is mandatory at both ends regardless of scope flags.
Incident intake may retain the original description as location text even when no coordinates
were supplied; that record remains excluded unless its location scope is explicitly approved.

An excluded record is omitted whole. The exporter never coarsens, truncates or redacts a payload
while retaining its original producer revision: that would conflict with the digest of the same
revision received later over radio. Destination current peer/module/board/geographic policy is
still authoritative; a signed declaration cannot expand it. Original-producer notes retain the
existing `incident_updates:1` / `reconciliation:2` negotiation and parent-review constraints.
Replica copies are not re-signed as new original-producer records.

## Format version 1

The file is UTF-8 JSON with exactly `format`, integer `version`, `core`, `public_key`, `signature`.
`format` is `outpost-physical-transfer`; version is `1`. `core` is standard base64; keys/signatures
are lowercase hex of respectively 32 and 64 bytes. The core object has exactly `origin`,
`destination`, `created_at`, `scope`, `items`. Scope contains exactly boolean `public_records_only`
(must be true), `public_labels`, `precise_locations`. Every item has `stream`, `uid`, `epoch`,
`revision`, `payload`. Item UIDs belong to the signing origin; epochs are the retained 32-hex
producer lineage and revisions positive integers, never clock-based conflict winners.

The existing Ed25519 relay-origin private key signs the exact decoded core bytes prefixed with
`outpost-physical-transfer-v1` and a NUL byte. No new signing-key hierarchy is created. Outpost
emits sorted-key, compact JSON with standard JSON Unicode escaping and no non-finite numbers.
Receivers verify the exact signed bytes before interpreting content and compute replay identity
as SHA-256 of the same prefix, public-key bytes, and canonicalized core. Harmless outer JSON
whitespace does not bypass replay protection. Duplicate keys or record identities, unknown
versions/fields/streams, invalid numeric/text types, excessive nesting, and non-finite values fail.

Limits: 192 KiB whole file, 128 KiB decoded core, eight items, 12,000 encoded payload bytes per
item, 160-character record identities, ten nesting levels, and 64 incident-origin provenance IDs.
There are no compression, archive extraction, filesystem paths, network references or executable
instructions in the format. Normal source payloads that cannot fit fail or remain excluded;
this is not arbitrary-size backhaul. The raw HTTP upload is bounded while streaming and before
JSON parsing, with a 30-second upload deadline; the browser checks size before loading the
selected file and limits requests to 30 seconds. Changing export scope resets the page cursor.
An expired browser request is an unconfirmed outcome, not proof of rollback.

## Transaction, replay and time contract

`FederationBundleService` borrows the existing database, peer service and sync owner. Initial
identity commissioning is a named audited decision. Export preview/signing uses the writer to
observe current eligible payloads and their revision heads consistently; signatures use the
existing persisted key. The export review tag binds the page, privacy selection, key, lineage and
current peer policy. Export audit contains only digest, scope, peer, key fingerprint and count.

Import parsing is bounded outside the writer. Within one transaction, the service rechecks the
current session/account (including revocation/expiry), local identity, active pairing, trusted
key, current scope, receipts and reviewed content. The import comparison tag additionally binds
inbox decisions and the local revision watermark. It is a comparison tag, not authorization or
proof that a human read every field. No lock is held across uploading, human review or radio I/O.

Approval calls the existing `quarantine_transaction` and `import_inbox_transaction`, plus the
existing per-item human-review audit, using that same writer. All imports, source revision
receipts, review decisions, a bundle receipt and summary audit commit together. No alternate
database/importer, background worker, pager, ACK, ownership acceptance, or radio sender is added.
Imported alerts retain the existing inert-import settings; they do not authorize broadcasting.

Newer received revisions are never overwritten by an older file. Same-revision conflicts and
lineage changes fail for separate reconciliation review. An equal pending radio item can be
explicitly imported; an already imported/rejected item is skipped. If payload retention removed
the inbox but retained its revision receipt, the file is skipped rather than resurrecting it.
Bundle receipts retain metadata only, survive peer deletion, and have a hard 4,096-entry admission
limit. At capacity, import fails closed: no time pruning silently restores replay admission.
Existing per-peer item quotas also apply. Operators must plan this finite capacity; unrestricted
bulk transfer and automated receipt compaction are not implemented.

An interrupted transaction rolls back; interruption during/after commit may leave a successful
commit. Re-preview detects that state without duplicate actions. Previously committed pages
remain committed if another page fails, so resumability is page-granular, not a multi-file atomic
snapshot. Retain carrier files until both operators verify the needed records locally.

Creation timestamps are advisory provenance only. Clock steps do not expire a signature or order
replicas: trust uses the current pin and reconciliation uses producer lineage/revision. Rotation
requires current-key verification through the existing workflow; an older allegedly pre-revocation
signature does not bypass a revoked/replaced pin. Session expiry and hourly quotas still use their
existing clocks. Restoring an old database can lose receipts/revisions that existed after that
backup; no offline file can prove those lost decisions. Fenced recovery remains #145/#146.

## Verification boundaries

`tests/unit/test_bundle_format.py` exercises the production parser's malformed/signed/oversized
input boundaries. Integration tests use two temporary real Outpost stores and existing import,
revision, framing and signing code, with synthetic radios and keys. They test radio-off identity
restoration, unchanged radio digests, subsequent changed-revision reconciliation, notes, boards,
alerts, privacy, current authority/pins, concurrent reviews, quota and receipt limits, and faults
at quarantine/domain/audit/receipt/commit boundaries. Actual Chromium tests cover signed downloads,
uploads, previews, CSRF/roles, narrow layouts, XSS-safe text, lost replies and no automatic retry,
with all external requests blocked. CI enforces 90% combined and per-file coverage for the parser,
service and routes without lowering any existing floor.

This does not qualify physical USB media, field radios, firmware, power loss, long-duration
storage, a cold installed appliance, trusted UTC, or a human field drill. #141's clock work remains
independent; avoiding wall-clock revision ordering does not certify its hardware. No migration,
deployment, live-store read or radio operation is performed merely by adding this software.
