# Node loss, replacement identity and resident mobility

Decision for #146, 2026-09-07. Selective federation is **not a hot standby**.
The supported encrypted off-device recovery path is an isolated, identity-fenced
archive workbench. A fresh serving replacement is a new station with new trust.
This decision approves no automatic private-data replication, portable account,
roster migration or clone-activation feature. Consequently there is no new
implementation issue to open for any of those unapproved features.

## What survives, and where

“Survives” below means a committed, readable copy actually exists. A queued send,
transport acknowledgment, custody receipt or old successful backup job is not
proof of a currently retained, human-reviewed record on another appliance.
See [retention](RETENTION.md) for the actual configured policies; encrypted copies
have a separate operator-managed retention and destruction obligation.

| Data class | Surviving intact source | Separately stored federation copy | Verified encrypted checkpoint | If source and all copies are lost |
| --- | --- | --- | --- | --- |
| Public BBS threads/posts | Original content and provenance, subject to board retention | Only permitted/received records; quarantine is not publication; original provenance retained | Content present at checkpoint, including private boards | Uncopied content unrecoverable |
| Incidents and neutral incident notes | Source authority, revisions, references and history | Only permitted records actually received; operator review and local retention apply; not the source's authority | Checkpoint records, revisions, lineage and retired-reference ledger | Uncopied changes unrecoverable; recollection is a new attributed report |
| Alerts and ACKs | Local alert/audience/ACK and escalation evidence | Approved alert copies where policy permits; not a backup of local audiences or ACKs | Local checkpoint state only, with escalation disabled by the fence | No inference that a person received or acted on an alert |
| Responsibility offers, accepted owners, handoffs | Local current state and event history | Not federated | Private checkpoint state; not a fresh acceptance or permission to dispatch | Re-establish responsibility explicitly; never mark unknown work complete |
| Private mail/conversations | Local messages until retention/pseudonymization | Only any separately authorized targeted mail delivery; not general replication or a mailbox backup | Checkpoint messages, identities and conversation state | Missing messages unrecoverable; a sender's copy is not delivery proof |
| Welfare check-ins, rosters, exact positions | Local event/provenance/retention rules | No automatic welfare/position replica | Checkpoint history only; later status, withdrawals and arrivals unknown | Missing is unknown, never automatically “safe,” absent or noncompliant |
| Mesh members, fingerprints, trust and handles | Locally established identity decisions | Discovery and a shared radio ID do not copy local membership authority | Historical decisions, not current revocation/exclusivity proof | Enrol afresh and verify identity; do not promote by name or ID match |
| Web accounts, passwords and MFA | Local accounts and current sessions | Not federated; independent of mesh membership | Account verifiers/MFA material preserved; copied sessions and pending enrolments discarded | New named accounts; no default shared credential or automatic mesh-to-web mapping |
| Pairing/signing keys, replay counters, idempotency receipts | Retained in the same database/key lineage | Peers retain their own independent trust/counters/receipts | Whole checkpoint preserved, but potentially behind peers or later effects; cannot activate | New identity and explicit new pairing; never reset counters under the old key |
| Runtime configuration and selected secrets | Local configuration plus runtime settings | No automatic configuration/secret replica | Effective config, intents, selected credentials and optional operator-supplied radio profile | Recommission explicitly; no guessed credentials/channel settings |
| Maps, models, OS, package/lock files, AP/DNS/TLS client trust, radio firmware/device secrets | Separate host/device assets | Not federation backups | Not implicitly captured; supported TLS files/profile only as documented | Use the independently prepared [replacement kit](ENCRYPTED-RECOVERY.md); otherwise unavailable |

Local database-only snapshots on the same SD card are not protection against card,
appliance, fire or theft loss. A private checkpoint predating a privacy withdrawal
still contains that data; it is not permission to resurrect or redistribute it.

## Recovery objectives, not unearned qualification

These are operator planning targets for the community, not measured guarantees:

| Failure | Recovery-point objective (maximum intended loss) | Recovery-time objective and gate |
| --- | --- | --- |
| Power interruption, intact readable store | Last committed transaction under the [WAL/FULL contract](COMMIT-DURABILITY.md) on flush-honoring storage; actual acknowledged-ID retention remains physically unqualified | Local service within 5 minutes of stable power, only after health/time/radio checks; physical #136/#44/#148 remains open |
| Failed storage or lost appliance with an independent encrypted copy | No more than 24 hours of private/configuration history: make and verify an actual independent copy at least daily and after critical identity/configuration changes | Isolated operator archive review within 30 minutes with compatible prepositioned runtime, custody and a trained operator; field timing remains #147 |
| Fresh serving replacement | New writes from commissioning onward; no inherited source authority | Basic local communication and new incident intake within 60 minutes of a prepared kit reaching a safe powered site; physical #147/#148 prerequisites, not a software guarantee |
| Origin unavailable but a second peer retained public records | Last actually received/reviewed record, not “all records”; target a reviewed checkpoint at least every 15 minutes during active incidents where channel capacity permits | Continue the surviving node's independent local services; source updates remain unknown until verified reconciliation |
| No usable copy, key/passphrase or compatible assets | Potentially total local history loss | No promised historical recovery time; establish a fresh station and use an explicitly labelled manual incident/welfare register meanwhile |

The 15-minute planning checkpoint is not a change to the G6 first-report latency
goal, nor a promise that RF capacity or available humans meet it. Record observed
replica/backup ages and the exact last verified record. During an untrusted-clock
period use signed producer revisions/lineage and a recorded event sequence, not
wall-clock “newest wins.” #141 still owns physical clock/RTC confidence.
If the last independent verified copy is older than the stated target, declare
the RPO breached; do not backdate evidence or erase old records to fit a backup.

## Replacement and old-node reappearance

1. Record the outage and last known source identity, public signing fingerprint,
   copied revisions, last receipt/counter evidence and backup digest. Treat the
   old appliance as possibly alive or compromised until independently established
   otherwise. Do not issue an ACK, incident completion or welfare status on its behalf.
2. Quiesce authorized operations involving the old station at each reachable
   community peer. Review/cancel outstanding local deliveries explicitly, preserve
   receipts/history and reject the predecessor pairing **and** origin signing pin.
   Review other origins/paths separately; a direct pairing and an end-to-end origin
   pin are different authorities. Trust changes cannot unsend an already handed-off
   frame or undo an already performed action.
3. Restore the encrypted copy only with the [recovery workflow](ENCRYPTED-RECOVERY.md)
   into a new protected directory. Review its authenticated schema, identity and
   record counts with a named operator. The persistent fence disables RF, providers,
   background workers and ordinary APIs, including sending/import/export. Restarting
   does not clear it. Preserve original keys/counters/queues as evidence, never reset
   them to make a clone communicate.
4. Commission a **fresh** serving station with a distinct mesh identity, newly
   generated signing/pairing keys, locally verified configuration, new named web
   accounts and current access decisions. Verify its identity/fingerprints out of
   band, mutually pair it as new, and reapprove scopes and quotas at every peer.
   A shared station name is a label, not identity proof. A former node's private key
   in a backup proves possession, not unique possession or current authorization.
5. Keep old records attributed to their original producer. Review independently
   available public records through normal policy-controlled imports. New reports
   on the replacement are new producer records, even if local suffixes, titles or
   clocks match. Do not reconstruct old source revisions, move private mail/rosters,
   or resend stale actions. Obtain current human confirmation for new assignments,
   consent or alerting decisions, recording any unresolved prior outcome.
6. If the old node reappears, its rejected peer entry stays rejected; forgetting and
   rediscovering it creates a **pending**, not trusted, peer. New origin keys are
   candidates, not silent pin replacements. Review its records as independent,
   potentially stale evidence. Do not automatically merge two streams or choose by
   wall clock. Preserve conflicts and ask the responsible operator/resident.

There is deliberately **no off-device clone activation or fence-removal command**
in this release. A full historical archive can be inspected by its authorized host
operator, but it is not a ready-to-transmit replacement. In-place database rollback
on an ordinary station is also not permission to reuse a stale key/counter lineage;
it needs quiescence and separate reconciliation of already-delivered work. Do not
bypass the supported path by copying an archived raw database into a fresh instance.

No partitioned system can prove global single-node ownership from a copied key
alone. A peer that has not received an operator's retirement decision may still
trust the old physical node. The supported guarantees are local explicit trust,
distinct fresh identities and a nontransmitting restored copy—not global revocation
during a partition, protection against a hostile host operator, or exactly-once
human action. Retain an offline retirement record and obtain each peer's decision
when reachable. If exclusive authority or previous delivery is unknown, keep it
unknown and do not automatically repeat the sensitive action.

## Existing legacy BBS namespace association

Federation → Content identity → Adopt history is narrowly a legacy public BBS
namespace convenience, **not** the replacement procedure above. Only use it after
independently verifying continuation of the *same* original BBS record namespace.
A fresh empty node with reused numeric suffixes must never adopt old suffixes.
This control cannot infer namespace continuity from a label or key.

The operator must first reject/remove the predecessor peer and reject/remove its
origin signing pin, then mutually pair the successor under its own identity. A
preview shows the public BBS counts, both IDs and scope. A separate explicit
confirmation carries a process/session-bound review token (at most ten minutes;
boundary/restart or any relevant context change requires another preview).
Inside one existing database transaction, the service rechecks current enabled
modules, named account, unexpired session, role, password-change state, both
pairing approvals/key, predecessor retirement, pin, history and association state.
The association and shared redacted audit commit together. No key, counter,
session, private record, incident, alert, accepted responsibility or queued action
is transferred. There is no upsert, silent reassignment, multiple-predecessor
alias, chain or automatic retry. Older ambiguous associations fail closed at BBS
lookup instead of choosing an arbitrary origin; normal producer provenance stays.

## Resident continuity is voluntary and local

- **Consent:** explain the destination, purpose, exact fields, intended viewers,
  retention and limits before a resident volunteers a new local report or asks
  an operator to consult an old private record. Declining historical transfer
  must not prevent new local help/intake. No bulk consent inferred from evacuation,
  incident urgency, mesh presence, former membership or possession of a backup.
- **Minimal provenance:** create a new local enrolment/check-in and record its
  source as the current resident statement. If voluntarily referring to earlier
  history, record the old station/event/reference and its unverified age without
  silently claiming an authoritative imported result. Avoid copying free text,
  positions, contact lists or another person's statements unless separately needed
  and authorized. No automatic transfer feature is implemented or approved here.
- **Identity separation:** authenticate a radio fingerprint through the existing
  PKI review and local trust process. The same radio at another node initially
  remains guest/pending. Handles and numeric mesh IDs do not confer membership,
  responder responsibilities, web account access, password/MFA continuity or group
  membership. Replacement radio/key conflicts require current operator verification.
- **Conflicts:** preserve separately attributed old/new statements and event IDs.
  A new “OK” at the destination does not automatically close an old “need help,”
  mark the old roster accounted for, cancel an assignment or prove arrival elsewhere.
  Ask the resident/responsible operator; record uncertainty rather than silently
  applying last-write-wins across clocks or communities.
- **Revocation:** accept local withdrawal/pseudonymization through existing member
  privacy controls. Notify another custodian only through an authorized minimal
  request when contact becomes possible. Offline revocation cannot be immediate
  everywhere; do not claim it is. Consult current withdrawal records before using
  old checkpoint content; when current consent cannot be established, hold it.
- **Retention:** destination data follows its disclosed local policy; source data
  follows the source policy and explicit deletion decisions, not presumed mobility.
  Preserve necessary de-identified audit/reference evidence. Record custody and
  expiry of every encrypted checkpoint; destroy copies under the community's
  stated policy. Deleting a live row does not delete an old independent backup.

## Evidence and qualification boundary

`tests/integration/test_node_loss_drill.py` uses real isolated application stores,
signed reviewed public transfer, an actual encrypted off-device checkpoint, source
shutdown, fresh fenced startup and named-operator authentication. Its fixture has
two public incidents surviving on a second node, one incident and one private
check-in in the earlier checkpoint, one later private check-in and one uncopied
incident unrecoverable. Reappearance/enrolment does not copy trust, private mail,
web accounts or rosters; a voluntary new check-in is a distinct local event.
`test_node_loss.py` checks current authority, replay continuity, stale/competing
reviews, atomic audit, ambiguous legacy mappings and incident/alert independence.
Real Chromium tests cover cancel, stale-key conflict and explicit fresh consent at
320/1280 pixels without external network requests or simulated RF transmissions.

These checks do not certify a real appliance power cut, SD controller durability,
field coverage, resident usability, physical recovery-kit completeness or the
target recovery times. Those existing gates remain #44/#135/#136/#147/#148/#155/#157;
closing this architecture/software contract does not close those physical gates.
