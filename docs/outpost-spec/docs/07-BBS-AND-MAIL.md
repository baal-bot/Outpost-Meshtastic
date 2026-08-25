# 07 — BBS & Mail

**Status:** Baseline · **Phase:** 1 · **Prerequisite:** [05-DATA-MODEL.md](05-DATA-MODEL.md)
**Implements:** `src/outpost/bbs/`

This is the MVP. Everything else is layered on top. If Phase 1 ships and nothing else ever
does, the community still has something worth running.

---

## 1. What a BBS means on a 1 kbps link

The 1990s BBS worked because the terminal was fast and the content was local. Here the
content is still local, but the terminal is a 200-byte packet with a 10-second round trip.
So the classic BBS interaction model inverts:

| Classic BBS | Outpost |
|---|---|
| Browse deep menus, read everything | Ask a narrow question, get a narrow answer |
| The session is the interface | The session is an accelerator; each message stands alone |
| Read every new message | Get a digest of what changed |
| Full message text always | Summary first, body on request |
| Push new content to logged-in users | Pull, always. Push only for alerts |

**REQ-BBS-001** — The BBS **MUST** be pull-oriented. The node **MUST NOT** transmit board
content that was not requested, with the sole exception of subscription digests, which are
opt-in, rate-limited, and DM-only.

---

## 2. Boards

**REQ-BBS-002** — Boards **MUST** be operator-defined from the dashboard. A default set
**MUST** be seeded on first run:

| Slug | Title | Purpose |
|---|---|---|
| `gen` | General | Catch-all |
| `roads` | Roads & Access | Closures, conditions, hazards |
| `lost-found` | Lost & Found | Pets, gear, people's stuff |
| `swap` | Swap & Give | Trade, lend, give away |
| `events` | Events | What's happening and when |
| `help-wanted` | Help Wanted | Requests for a hand |
| `notice` | Notices | Operator/official announcements — post restricted |

**REQ-BBS-003** — Each board **MUST** carry independent `min_read_trust` and
`min_post_trust`. `notice` defaults to `min_post_trust = operator`.

**REQ-BBS-004** — Board slugs **MUST** be ≤16 bytes, `[a-z0-9-]`, and **MUST NOT** collide
with any command name or alias in the registry (doc 04 §10) — which is why the "help wanted"
board is `help-wanted` and not `help`. The dashboard **MUST** validate this at creation time
against the live registry, and a startup check **MUST** fail loudly if a newly-added command
collides with an existing board slug.

**REQ-BBS-005** — `BOARDS` **MUST** render every readable board with an unread count in one
part where possible, ordered by unread count then `sort_order`. Boards with no unread items
and no activity in 30 days sort last and **MAY** be elided behind `MORE`.

---

## 3. Threads and posts

**REQ-BBS-006** — A thread has a subject (≤64 chars) and an ordered sequence of posts. The
first post is `seq = 1`.

**REQ-BBS-007** — When a user creates a thread with `POST <board> <text>` and supplies no
explicit subject, the subject **MUST** be derived from the first sentence or first 48
characters of the body, truncated at a word boundary. Requiring a separate subject prompt
would violate the two-round-trip ceiling (REQ-CMD-015).

**REQ-BBS-008** — An explicit subject **MAY** be given with a leading `#`:
`POST roads #Mill Rd bridge / Culvert washed out, impassable both ways.` The `/` separates
subject from body.

**REQ-BBS-009** — Over-the-air post bodies are limited by the packet, so ≤200 bytes in
practice. Posts created from the dashboard **MAY** be up to 1000 characters, and when such a
post is rendered over the air it **MUST** be summarised to the part budget with a `…MORE`
affordance, never silently truncated.

**REQ-BBS-010** — Thread listing **MUST** render, per thread, in ≤55 bytes:
`<n> <subject≤28> <replies>rep <age> @<author>`. Ages use compact relative form:
`4m`, `2h`, `3d`, `2w`.

**REQ-BBS-011** — Reading a thread (`R <n>`) **MUST** return the opening post plus a count of
replies, not the whole thread. Replies are fetched with `MORE`. Rationale: a 12-reply thread
is 12 packets and 25 seconds of channel time that the reader did not ask for.

**REQ-BBS-012** — `R <n>` **MUST** push a `THREAD` context frame so that a bare `RE <text>`
replies to it.

**REQ-BBS-013** — Thread numbers shown in a listing **MUST** be **listing-relative** and
bound to the session's page cursor, not global IDs. `R 1` means "the first one you just
showed me". Global UIDs are never typed by users.

**REQ-BBS-014** — Posting **MUST** be acknowledged in one line with the assigned reference
and nothing else: `✓ roads#42.` The acknowledgement **MUST NOT** echo the post.

**REQ-BBS-015** — A post to a thread the user is not currently in **MUST** be possible in one
message: `RE 42 Bridge is open again.`

---

## 4. Unread tracking and digests

**REQ-BBS-016** — Read state **MUST** be tracked per member per scope via `read_marker`
(doc 05 §4). A member's "unread" is everything created after their marker for that scope.

**REQ-BBS-017** — `NEW` **MUST** return a cross-board digest of everything new since the
member's last check, in one part where possible:

```
> NEW: roads 3 (bridge open, plow sched, tree Cedar), lost-found 1 (grey cat), inc 2 active.
```

**REQ-BBS-018** — `NEW` **MUST** advance the member's read markers. A second `NEW` with no
intervening activity returns `Nothing new.` in one line — 12 bytes, and the correct answer.

**REQ-BBS-019** — Digest subscriptions (`SUB <board>`) support three cadences:

| Cadence | Behaviour |
|---|---|
| `on_request` (default) | Nothing is transmitted; content appears in `NEW` |
| `daily` | One DM digest at the member's configured hour, `digest` class, quiet-hours-deferred |
| `immediate` | A DM when a new thread (not reply) is created. **Hard-capped** at `bbs.immediate_max_per_hour`, default 3 |

**REQ-BBS-020** — `immediate` cadence **MUST** require trust ≥ `member` and **MUST** be
disableable node-wide by the operator, because it is the one BBS feature that can generate
unsolicited airtime at scale.

**REQ-BBS-021** — Digests **MUST** be coalesced: one digest per member per cadence period
containing all boards, never one per board.

**REQ-BBS-022** — A member marked `unreachable` (doc 03 §8) or `STOP`-muted **MUST NOT**
receive digests. Digests resume on their next inbound message.

---

## 5. Search

**REQ-BBS-023** — `SEARCH <terms>` **MUST** use FTS5 BM25 over readable, non-hidden posts,
ranked with the same recency weighting as AI retrieval (doc 06 §4.2).

**REQ-BBS-024** — Search results **MUST** render as `<board>#<n> <snippet≤40> <age>` and
**MUST** be limited to one part by default.

**REQ-BBS-025** — An empty result **MUST** suggest the nearest alternative in one line:
`No match. Try SEARCH bridge, or B roads.`

---

## 6. Moderation

**REQ-BBS-026** — Moderation **MUST** be a soft hide with actor and reason recorded
(doc 05 §4). Hidden posts remain in the database and remain visible to the operator on the
dashboard, marked.

**REQ-BBS-027** — Hiding a thread's opening post **MUST** hide the whole thread. Hiding a
reply **MUST NOT** renumber subsequent replies — `seq` is immutable.

**REQ-BBS-028** — `OP RM <ref>` over the radio **MUST** work in one message and **MUST**
require trust `operator`. All other moderation (lock, pin, move, edit) is dashboard-only.

**REQ-BBS-029** — The author of a post **MUST** be able to delete their own post within
`bbs.self_delete_minutes` (default 30) with `RMPOST <ref>`. After that window it requires
operator action, because other people may have already read it and replied.

**REQ-BBS-030** — Rate limits by trust level **MUST** be enforced (doc 12 §5). Defaults:

| Trust | Posts/hour | Threads/hour | Mail/hour |
|---|---|---|---|
| `guest` | 0 | 0 | 0 |
| `member` | 12 | 4 | 8 |
| `trusted` | 30 | 10 | 20 |
| `responder` | 30 | 10 | 20 |
| `operator` | unlimited | unlimited | unlimited |

**REQ-BBS-031** — The **first** rate-limited action in a bucket's window **MUST** return one
line stating the limit and when it resets:

```
> Limit. 12 posts/h. Resets 14:22.
```

Subsequent over-limit messages in the same window are discarded silently (REQ-SEC-019) —
one informative reply is service, five are the node amplifying the flood it is trying to
stop.

---

## 7. Mail

**REQ-BBS-032** — Mail is **store-and-notify**. `SEND <handle> <text>` stores the message and
returns `✓ Sent to @dana.` The body is **not** transmitted onward at that moment.

**REQ-BBS-033** — The recipient **MUST** be notified in the cheapest way available, in this
order:

1. **Piggy-back**: appended to the recipient's next outbound response as a ≤24-byte suffix
   (`· 1 mail`).
2. **Digest**: included in their next `NEW` or scheduled digest.
3. **DM notification**: only if the recipient has `mail.notify: immediate` set and has been
   heard within `mail.notify_window_hours` (default 12), and only within airtime budget.

**REQ-BBS-034** — Mail bodies **MUST** be transmitted only on explicit `READMAIL`.

**REQ-BBS-035** — Mail to an unknown handle **MUST** be accepted and held as `queued` with an
unresolved `to_label`, for `mail.hold_unknown_days` (default 14). If the handle is claimed in
that window the mail binds to the new member. Otherwise it expires and the sender is notified
once, piggy-backed.

**REQ-BBS-036** — Mail **MUST** expire per `store.retention.mail_days` (default 180) and
unread mail **MUST** be reported to the sender as `expired` rather than silently vanishing.

**REQ-BBS-037** — Mail **MUST NOT** be a retrieval source for the AI, ever, for anyone,
including the operator's own AI queries (REQ-AI-020).

**REQ-BBS-038 (privacy honesty)** — `HELP MAIL`, the dashboard, and the first-use response
**MUST** state plainly: *mail is private from other members, not from the node operator.*
The node stores plaintext. Claiming otherwise would be a lie the architecture cannot support
(doc 12 §7).

**REQ-BBS-039** — `MAIL` inbox summary **MUST** fit one part:
`> 3 mail: 1 @ray 2h, 2 @jo 1d, 3 @kit 3d. RM <n> to read.`

---

## 8. Channel directory

Carried over from TC²-BBS and ZephyrGate because it solves a real problem: a newcomer with a
handheld does not know which channels the community uses.

**REQ-BBS-040** — The node **MUST** maintain a directory of community channels: name, a
description, and — only when the operator explicitly marks a channel as public — its PSK.

**REQ-BBS-041** — `CHANS` **MUST** list channel names and descriptions. PSK distribution
**MUST** require an explicit second command (`CHAN <name>`), trust ≥ `member`, and **MUST**
be rate-limited and audit-logged.

**REQ-BBS-042** — The node **MUST NOT** transmit a PSK on the public channel under any
circumstance — DM only.

---

## 9. Rendering reference

An implementing agent **MUST** match this density. Every line below is under 200 bytes.

```
BOARDS
> roads 3new · lost-found 1new · gen · swap · events · help-wanted · …MORE 1

B roads
> roads · 12 threads
1 Mill Rd bridge out    4rep 2h  @dana
2 Plow schedule 24-25   1rep 9h  @ray
3 Tree down Cedar Ln    0rep 1d  @jo
4 Weight limit posted   2rep 3d  @ray
5 Grader on Hollow      0rep 5d  @kit
…MORE 7

R 1
> roads#42 Mill Rd bridge out · @dana 2h
Culvert washed. Impassable both ways. Detour Hollow Rd +8min.
4 replies · MORE to read · RE <text> to reply

MORE
> #42 replies 1-3/4
1 @ray 2h: Town notified, no ETA.
2 @jo 1h: Hollow Rd is soft, 4wd only.
3 @dana 40m: Cones up both ends.
…MORE 1

POST roads Bridge is open again, one lane.
> ✓ roads#47

RE 42 Confirmed open one lane as of 16:10.
> ✓ roads#42.5

RE roads#42 Confirmed open one lane as of 16:10.
> ✓ roads#42.5

NEW
> NEW roads 3 · lost-found 1 · 2 inc active · 1 mail

SEARCH bridge
> roads#42 Mill Rd bridge out 2h · roads#31 Bridge weight limit 3w · gen#12 … 3w

SEND ray Plow got the top of Hollow Rd, bottom still drifted.
> ✓ Sent to @ray

MAIL
> 2 mail: 1 @ray 2h · 2 @jo 1d · RM <n> to read

RM 1
> @ray 2h
Can you check the culvert at the top of Hollow when you're up there?
RR <text> to reply
```

---

## 10. Acceptance criteria (Phase 1 exit)

Every item **MUST** be demonstrated in the mesh simulator **and** on hardware.

| # | Criterion |
|---|---|
| 1 | A new peer sends `BOARDS` and receives a readable board list in one packet, having never registered |
| 2 | `NAME dana` claims a handle; `WHOAMI` reflects it |
| 3 | `POST roads <text>` creates a thread and acknowledges in one packet |
| 4 | `B roads` → `R 1` → `RE <text>` completes a threaded reply in 3 messages with no menu navigation |
| 5 | `RE 42 <text>` replies from a cold session with no prior context |
| 6 | `NEW` reports cross-board unread accurately and advances markers; a second `NEW` returns `Nothing new.` |
| 7 | `SEND`/`MAIL`/`RM` round-trips; the body is transmitted only on `RM` |
| 8 | Mail to an unclaimed handle binds correctly when that handle is later claimed |
| 9 | `SEARCH` returns ranked results excluding hidden posts and unreadable boards |
| 10 | Rate limits fire at the configured thresholds with an informative one-line response |
| 11 | `OP RM` hides a post; it disappears from listings, search, and AI retrieval; the audit row exists |
| 12 | A 40-thread board paginates correctly through `MORE`, and an expired cursor is handled gracefully |
| 13 | **No response in the entire test run exceeds its part budget** |
| 14 | **24-hour soak on a simulated 12-member mesh keeps node airtime under 8%** |
| 15 | All of the above passes with the WAN interface down and the AI module disabled |
