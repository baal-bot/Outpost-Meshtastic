# Incident responsibility and accepted handoffs

Implementation for [#151](https://github.com/baal-bot/Outpost-Meshtastic/issues/151).
This is local coordination software, not physical delivery qualification or an emergency-service guarantee.

## Meaning of each decision

| Decision | Who may make it | Effect |
| --- | --- | --- |
| Offer / handoff | Named web operator, verified radio operator, or accepted owner | Creates/replaces a pending offer with a next action. Any accepted owner remains responsible. |
| Accept | Offered operator account, offered verified responder, or a current verified member of the offered team | Explicitly replaces the accepted owner and adopts the offered next action. An administrator cannot accept on another person's behalf. |
| Update | Accepted person/account or current verified member of the accepted team | Records the next action and last-verified receive time. |
| Cancel offer | Operator or accepted owner | Removes the pending offer; accepted ownership is unchanged. |
| Release | Operator or accepted owner | Explicitly removes ownership and any pending offer. The gap is visible. |
| Complete responsibility | Accepted owner only | Ends this responsibility; it does not resolve the incident. |

An alert ACK, RF ACK, application receipt, quarantine import, report confirmation,
handover-page read, incident resolution or incident expiry **never** accepts or completes
responsibility. Terminal incidents may retain unfinished responsibility; new offers require
reopening the incident. An already-pending offer may be accepted only after reviewing its
current incident state. Merge is refused while either incident has an owner or pending offer;
explicitly release/cancel first. Retained decision history remains with its original incident.

Team acceptance is attributable to the acting member, not every member of the team.
Any *current* eligible team member may maintain or hand off the team's responsibility.
A named operator account may accept its own assignment without a radio. To act for a radio
person/team from the web, it needs the existing administrator-reviewed link to that eligible
radio identity. Removing membership, changing trust/key eligibility, disabling an account,
revoking its session, or deleting a target is rechecked at the writer boundary. Unavailable
owners are shown and retained until explicit operator release; they are not silently replaced.

## Local authority and partitions

Each Outpost owns only its own coordination record for the stable canonical incident UID.
It cannot prove globally exclusive responsibility across disconnected Outposts. Incident/note
federation, peer receipts and delayed operator-reviewed incident facts do not export or mutate
this responsibility state. Coordinate a common lead Outpost before a response, and reconcile
each station's human responsibility explicitly after a partition. Do not infer cross-Outpost
handoff acceptance from identical names or local incident numbers.

The offer is retrieved through the web record or a verified `TASK` DM. There is no automatic
radio page or delivery claim when an operator saves an offer. Use the existing approved
notification workflow if responders need paging, then obtain explicit `TASK`/web acceptance.
Unaccepted offers remain visibly pending, including if the responder cannot reach this Outpost.
Physical team availability, message latency and unattended usage remain field qualifications.

## Operator and handheld workflows

Open an incident's **Handover** / **Timeline** record. The responsibility section shows the
accepted owner, pending target, both next actions, verification time, unavailable targets and
decision history. Choose a permitted decision, explicitly choose an eligible target for an
offer, enter the next action and confirm that you reviewed the displayed state. Refreshing
discards the form's prior review; nothing is submitted automatically. A missing/denied report
is not an unassigned/all-clear result.

Verified responder/operator PKI direct messages use:

```text
TASK <incident-number>
TASK <incident-number> TARGETS group
TASK <incident-number> TARGETS member [after]
TASK <incident-number> TARGETS account [after]
TASK <incident-number> offer <current-token> group:<catalog-id> <next action>
TASK <incident-number> accept <current-token>
TASK <incident-number> update <current-token> <next action>
TASK <incident-number> cancel <current-token>
TASK <incident-number> release <current-token>
TASK <incident-number> complete <current-token>
TASK <incident-number> NEXT
```

`TASK <number>` also provides numbered acceptance/cancel/release/completion choices when
authorized, and is linked from the incident TUI. Catalog IDs are non-reusable coordination
identities, **not** member/group/account database numbers. Retrieve them through `TARGETS`
or the web picker. Three target choices per handheld page, 25 per web page and at most
50 per service call bound lookups. Labels are not authorization. Next actions are one line,
at most 160 UTF-8 bytes; radio arguments are at most 220 bytes and replies at most three
existing governed parts. A token comparison failure requires a fresh `TASK` view.

Private `TASK` input and its TUI continuations bypass public emergency-keyword and
pending-position intake. A saved position cannot turn a private assignment into a public
incident. The normal router still enforces current trust, PKI DM, command/module policy and
rate limits; all replies use the existing governor and can be delayed/rejected/lost.

## Concurrency, time and recovery

`IncidentResponsibilityService` owns one existing SQLite writer transaction for current
identity/session/role checks, canonical identity, version comparison, ownership mutation,
attributable decision history and redacted audit. It performs no network or radio I/O.
There is no second database, worker, automatic retry timer or distributed lock.

Migration 183 adds current responsibility, decision history and non-reusable target identities.
Maintenance preserves a terminal incident while any accepted owner or pending offer remains,
even when its normal history window has elapsed. Explicit release/completion and cancellation
must remove that responsibility first; incident resolution never silently erases an unfinished
handoff. Once eligible incident retention runs, state/history cascade with the incident, while
non-reusable target bindings and the normal redacted audit remain protected.
Target identities are backfilled and created by member/group/account insert triggers; deleting
and recreating a source row cannot rebind an old target selection, even with the same name and
database number. Foreign-key deletion makes old targets unavailable. Read-only catalog views
do not create target rows. Versioned decisions never rely on arrival order or wall-clock LWW.

The 96-bit review tag binds the process/time generation, canonical identity and complete current
decision/target view. It is a comparison token, **not authentication**. The sole writer rechecks
the tag and authorization, so concurrent or delayed decisions cannot overwrite an unseen owner.
Restarts and observed wall/monotonic discontinuities invalidate old tags and freshness while
retaining ownership. Last verified means the local receive time of acceptance/update, not proof
of accurate civil time or responder location. After 30 minutes it is stale; a timestamp alone
does not establish current availability. A corrected clock may be reverified explicitly, without
claiming RTC/holdover qualification (#141).

State, history and audit roll back together before commit. A cancelled request or lost response
can follow a successful commit; refresh instead of retrying automatically. Web stale/denied/lost
responses disable submission until a fresh review. A repeated committed token conflicts.
Recovery from an *older restored backup* can lose newer responsibility decisions: this workflow
cannot certify unseen history after rollback. Reconcile with the response lead before reassigning.
Physical database power-loss survival remains separately unqualified (#44). The software
policy is the [checked WAL/FULL contract](COMMIT-DURABILITY.md) (#137).

## Privacy, records and verification

These are operator/responder records, not public incident payload extensions. Public/member
SITREP and wallboard paths omit assignment details. Authorized deterministic briefings show
local responsibility, while incident details are omitted from AI narration input. Incident
reports, CSV and offline HTML include current responsibility even for an empty change window,
plus retained attributable decisions. They use the existing coordinate coarsening, unnamed-radio
suppression, CSV formula protection and HTML escaping; exports never contain mutation tokens.
The local record may contain private operational text: avoid credentials and unnecessary personal
details. Reports require operator access. Exports remain plaintext artifacts under existing policy.

Member-data counts include linked responsibility decisions. Approved removal redacts linked
next-action text and pseudonymizes displayed member identities; it invalidates verification
without pretending an unavailable owner released the task. Protected redacted audit and decision
structure remain. A redacted offer must be renewed with a current next action before acceptance.

Tests use actual Outpost construction, temporary databases, real ASGI/Chromium, simulated radios
and normal governors. They cover concurrent/replayed decisions, current identity/membership,
target reuse, time/restart uncertainty, rollback/post-commit cancellation, private intake, bounded
handheld commands, operator/CSRF/module boundaries, 320px/desktop forms, lost responses,
exports/removal and delayed reviewed federation. None of these tests deploy migration 183 to a
live station, transmit physical RF, activate held nodes or qualify human/physical handoffs.
