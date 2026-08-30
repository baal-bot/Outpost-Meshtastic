# 04 — Command Grammar & Session Model

**Status:** Baseline · **Prerequisite:** [03-MESH-TRANSPORT.md](03-MESH-TRANSPORT.md)
**Implements:** `src/outpost/router/`, `src/outpost/commands/`

This document defines the entire over-the-air user experience. It is the product surface —
if this is wrong, nothing else matters.

---

## 1. Design premise

Two established paradigms, both flawed on this medium:

**The modal BBS.** `M` → mail menu → `R` → read → `1` → message. Four round trips. On a mesh
with 3-hop flooding, each round trip is 5–30 seconds, sometimes minutes, sometimes lost.
Deep menus are unusable here. TC²-BBS works, but every interaction is expensive.

**The stateless slash-command bot.** `/wx`, `/ai what time is sunset`. One round trip, but
no notion of *where you are*, so anything conversational becomes an unwieldy single line:
`/reply board=roads thread=42 text=bridge is open again`.

**Outpost uses both, behind a first-class conversational interface.** A direct-message member can
send `?`, choose from one-screen capability areas, and answer guided prompts without learning
syntax. Global verbs still work at any depth with no preamble. A session may additionally carry a
context stack that supplies defaults, so a bare word does the obvious thing. Screen state and
context are accelerators, never prerequisites.

**REQ-CMD-001** — Every command **MUST** be executable as a single self-contained message
with no prior state. Context is an accelerator, never a prerequisite.

**REQ-CMD-002** — Context **MUST** be recoverable in one message: `WHERE` reports the
current context, and `HOME` clears it.

---

## 2. Invocation

| Situation | Requirement |
|---|---|
| Direct message to the node | No prefix required. Every message is an input. |
| Secondary channel with `bbs: full` or `ai: true` | Prefix token required, default `!` |
| Primary/public channel (index 0) | Prefix token required, **always**, no exceptions except the emergency keyword set (doc 08 §4) |

**REQ-CMD-003** — The invocation prefix **MUST** be config-driven (`router.prefix`,
default `!`) and **MUST** accept the node's short name as an alternate prefix
(`CRO help`), so multiple Outpost nodes on one mesh can be addressed individually.

**REQ-CMD-004** — On the public channel, a message addressed to a *different* node's short
name **MUST** be ignored entirely.

**REQ-CMD-005** — Commands **MUST** be case-insensitive. Arguments preserve case. Leading
and trailing whitespace is stripped; internal runs of whitespace collapse to one space.

**REQ-CMD-006** — The node **MUST** accept both `!cmd` and `!CMD` and `! cmd`, and **MUST**
tolerate the smart quotes and autocapitalisation that phone keyboards insert.

---

## 3. The session model

### 3.1 Session

```python
@dataclass
class Session:
    member_id: str                 # mesh node id
    channel: int                   # channel the session is on (DM sessions use -1)
    context: list[ContextFrame]    # the stack; may be empty
    pending: PendingAction | None  # a one-shot continuation awaiting a bare reply
    last_seen: datetime
    last_digest_at: datetime | None
    page_cursor: PageCursor | None # for MORE
```

**REQ-CMD-007** — Sessions **MUST** be keyed by `(member_id, channel)`. A member's DM
session and their session on a channel are independent.

**REQ-CMD-008** — Sessions **MUST** expire after `router.session_idle_minutes` (default 30)
of inactivity. Expiry clears context and pending actions but **MUST NOT** delete anything
persistent (drafts are persisted; see §6).

**REQ-CMD-009** — Sessions **MUST** be in-memory only, and **MUST** be reconstructible as
empty after a restart without user-visible data loss.

### 3.2 Context frames

A frame narrows the meaning of bare input:

| Frame | Set by | Bare input means | Defaults supplied |
|---|---|---|---|
| `BOARD(id)` | `B <board>` | new post to that board | board for `POST`, `READ` |
| `THREAD(id)` | `R <n>` from a board listing | reply to that thread | thread for `POST` |
| `MAIL(peer)` | `M <handle>` | send mail to that peer | recipient for `SEND` |
| `INCIDENT(id)` | `I <n>` | add an update to that incident | incident for `UPDATE`, `ACK` |
| `COMPOSE(kind)` | multi-step flows | next field of the flow | — |

**REQ-CMD-010** — The context stack **MUST** be at most 3 frames deep. Attempting to push a
fourth replaces the top frame.

**REQ-CMD-011** — A global verb **MUST** execute without disturbing the context stack unless
it explicitly changes context. `WX` inside `BOARD(roads)` returns weather and leaves you in
`roads`.

**REQ-CMD-012** — `BACK` pops one frame. `HOME` clears the stack. `WHERE` renders the stack
in ≤60 bytes, e.g. `In: roads > thread 42`.

### 3.3 Direct-message screens

A screen is a compact interaction contract, not a terminal emulator. It carries a title, semantic
result lines, numbered choices, an optional one-value prompt, and recovery actions.

```text
OUTPOST / MAIL
1 Open inbox
2 Send a message
3 How private is mail?
0 Home · ? Menu
```

**REQ-CMD-012a** — `?`, `HELP`, and `MENU` in a DM **MUST** open a capability- and trust-aware Home
screen. Disabled or unauthorized actions **MUST NOT** be advertised.

**REQ-CMD-012b** — Displayed numbers and labels **MUST** resolve only against the current screen. A
bare number without live screen state **MUST NOT** be guessed and **MUST** return a one-line `?`
recovery hint.

**REQ-CMD-012c** — Every screen **MUST** start with `OUTPOST / <TITLE>` and end with stable recovery
actions. Fixed discovery screens **MUST** fit one 200-byte application payload.

**REQ-CMD-012d** — Guided composition **MUST** ask for one conceptual value at a time. Exact global
commands retain precedence and cancel an unfinished prompt. `0`, `HOME`, or `?` **MUST** reconstruct
navigation after lost or stale state.

**REQ-CMD-012e** — Stateful screens are DM-only. Channel interactions remain terse and prefixed,
and `!?` **MUST** direct the member to DM the node.

### 3.3 Pending actions

Some flows need one more piece of information. Rather than a menu, the node asks a single
question and marks the session `pending`.

**REQ-CMD-013** — A `PendingAction` **MUST** carry: the action, its partial arguments, a
prompt, an expiry (default 10 minutes), and an `on_timeout` disposition (`discard` or
`save_draft`).

**REQ-CMD-014** — While a pending action exists, a bare message **MUST** be interpreted as
its answer. Any recognised global verb **MUST** still take precedence and **MUST** cancel
the pending action with a one-line notice only if the cancellation is not obvious.

**REQ-CMD-015** — There **MUST NOT** be any flow requiring more than **two** round trips for
a core action (post, reply, send mail, report incident, check in). Two is the ceiling; one
is the target.

---

## 4. Command catalogue

Notation: `<required>` `[optional]` `{one|of}`. Aliases separated by `/`.

### 4.1 Core (always available, all phases)

| Command | Aliases | Arguments | Result |
|---|---|---|---|
| `HELP` | `?` `H` | `[topic]` | Contextual one-screen help. Never more than 2 parts. |
| `WHERE` | `W?` | — | Current context |
| `HOME` | `..` `/` | — | Clear context |
| `BACK` | `<` | — | Pop one frame |
| `MORE` | `+` `M+` | `[n]` | Next page of the last paginated result |
| `WHOAMI` | `ME` | — | Your handle, trust level, node id, unread counts |
| `NAME` | `HANDLE` | `<handle>` | Claim/change your handle |
| `STOP` | `MUTE` | `[duration]` | Suppress all non-alert traffic to you |
| `START` | `UNMUTE` | — | Resume |
| `ABOUT` | — | — | Node name, operator, version, disclaimer |
| `PING` | — | — | Liveness check. Replies `pong <snr> <hops>`. Phase 0 |
| `PRIVACY` | — | — | What is and is not private (doc 12 §7) |

**REQ-CMD-016** — `STOP` **MUST** be honoured immediately and **MUST** suppress every class
except `alert`. Suppressing `alert` **MUST NOT** be possible — the operator may permit it
via config, but it is off by default and requires an explicit opt-in flag with a warning.

### 4.2 BBS (Phase 1) — see [07-BBS-AND-MAIL.md](07-BBS-AND-MAIL.md)

| Command | Aliases | Arguments | Result |
|---|---|---|---|
| `BOARDS` | `BL` | — | List boards with unread counts |
| `BOARD` | `B` | `<board>` | Enter board; list recent threads. Pushes `BOARD` frame |
| `READ` | `R` | `[n]` | Read thread n (or newest). Pushes `THREAD` frame |
| `POST` | `P` | `[board] <text>` | New thread. Board from context if omitted |
| `REPLY` | `RE` | `[n] <text>` | Reply to thread. Thread from context if omitted |
| `NEW` | `N` | — | Everything new since your last check, across subscriptions |
| `SUB` | — | `<board>` | Subscribe to a board's digest |
| `UNSUB` | — | `<board>` | Unsubscribe |
| `SEARCH` | `S` | `<terms>` | Search boards (FTS) |
| `RMPOST` | — | `<ref>` | Delete your own post within the self-delete window |
| `CHANS` | — | — | List community channels in the directory |
| `CHAN` | — | `<name>` | Get a channel's details, incl. PSK if published. DM only |

### 4.3 Mail (Phase 1)

| Command | Aliases | Arguments | Result |
|---|---|---|---|
| `MAIL` | `MB` | — | Inbox summary |
| `M` | — | `<handle>` | Enter a mail conversation. Pushes a `MAIL` frame |
| `SEND` | `SM` | `[handle] <text>` | Send private mail. Recipient from context if omitted |
| `READMAIL` | `RM` | `[n]` | Read message n |
| `REPLYMAIL` | `RR` | `<text>` | Reply to the last-read message |
| `DELMAIL` | `DM` | `<n>` | Delete a mail message |

**REQ-CMD-016a** — Deletion verbs **MUST** be distinct per object type: `DELMAIL` for mail,
`RMPOST` for a post, `OP RM` for operator post removal. A single overloaded `DEL` whose
meaning depends on context is prohibited — the one place where context-sensitivity is
unacceptable is a destructive verb.

### 4.4 Directory (Phase 1)

| Command | Aliases | Arguments | Result |
|---|---|---|---|
| `WHO` | `NODES` | `[filter]` | Recently-heard members, sorted by last heard |
| `FIND` | — | `<handle\|partial>` | Look up a member |
| `LAST` | — | `<handle>` | When and where a member was last heard |

### 4.5 Assistant (Phase 2) — see [06-AI-AGENT.md](06-AI-AGENT.md)

| Command | Aliases | Arguments | Result |
|---|---|---|---|
| `ASK` | `A` `AI` | `<question>` | Retrieval-grounded answer, `[AI]`-prefixed |
| `SUM` | — | `[board\|thread\|incident]` | Summarise the current or named context |
| `TR` | — | `<lang> <text>` | Translate (if the model supports it) |

**REQ-CMD-017** — On a DM, a message that matches no command **MUST** fall through to `ASK`
when the AI module is enabled and the channel policy permits, and to a one-line
`Unknown. Send ? for help.` otherwise. On any non-DM channel, unmatched input **MUST NOT**
fall through to the AI.

### 4.6 Community Watch (Phase 3) — see [08-COMMUNITY-WATCH.md](08-COMMUNITY-WATCH.md)

| Command | Aliases | Arguments | Result |
|---|---|---|---|
| `REPORT` | `RPT` | `[type] <text>` | File an incident; position auto-attached if known |
| `REPORT!` | `RPT!` | `[type] <text>` | File a new incident, bypassing duplicate detection |
| `INCIDENTS` | `IL` | `[radius_km]` | Active incidents near you |
| `INC` | `I` | `<n>` | Incident detail. Pushes `INCIDENT` frame |
| `UPDATE` | `UPD` | `[n] <text>` | Add an update to an incident |
| `CONFIRM` | `CF` | `[n]` | Confirm an incident you have also observed |
| `DISPUTE` | — | `[n] [note]` | Dispute an incident |
| `ACK` | — | `[n]` | Acknowledge an alert or incident |
| `CLEAR` | — | `<n> [note]` | Mark resolved (requires trust ≥ `trusted`) |
| `OK` | `CHECKIN` | `[note]` | Check in as safe |
| `HELPME` | — | `[note]` | Check in needing assistance |
| `ROSTER` | — | — | Check-in roster summary |
| `ROSTER?` | — | `[status]` | Roster names, paginated |
| `ALERT` | — | `<sev> <text>` | Raise an alert (requires trust ≥ `responder`) |

### 4.7 Environment (Phase 4) — see [09-WEATHER-AND-ALERTS.md](09-WEATHER-AND-ALERTS.md)

| Command | Aliases | Arguments | Result |
|---|---|---|---|
| `WX` | `W` | `[place]` | Current conditions + short forecast |
| `FC` | `FORECAST` | `[days]` | Forecast, terse |
| `WARN` | `ALERTS` | — | Active weather/CAP alerts for the area |
| `SUN` | — | — | Sunrise/sunset/moon (computed locally, no network) |
| `QUAKE` | — | `[radius]` | Recent seismic activity |
| `POS` | — | `[handle]` | Position, distance and bearing |
| `WP` | `WAYPOINT` | `<name>` | Save a named waypoint at your position |
| `WPS` | — | `[radius_km]` | List community waypoints near you |

### 4.8 Operator (all phases, trust ≥ `operator`)

| Command | Arguments | Result |
|---|---|---|
| `OP STATUS` | — | Airtime, queue depth, link state, module health |
| `OP MUTE` | `<handle> [duration]` | Silence a member |
| `OP RM` | `<post_id>` | Remove a post |
| `OP TRUST` | `<handle> <level>` | Set trust level |
| `OP BCAST` | `<text>` | Operator bulletin |
| `OP QUIET` | `{on\|off}` | Emergency airtime clamp: alerts only |

**REQ-CMD-018** — Operator commands over the radio **MUST** be limited to those listed
above. Configuration changes, board creation, and anything destructive beyond post removal
**MUST** require the dashboard. A spoofed node ID must not be able to reconfigure the node.

---

## 4a. Two numbering spaces — read this carefully

Users encounter two kinds of number and they **MUST** be syntactically distinguishable.

| Form | Meaning | Scope | Example |
|---|---|---|---|
| Bare small integer after a listing command | **Listing-relative index** into the page just shown | The session's current page cursor | `R 1`, `INC 2`, `RM 3` |
| `<board>#<n>` or a number ≥ the node's ref floor | **Stable reference** minted by the node | Node-wide, permanent | `roads#42`, `RE roads#42 <text>` |

**REQ-CMD-018a** — Commands accepting a reference **MUST** accept both forms. A bare integer
resolves against the session's live page cursor; a `board#n` form resolves globally and
works from a cold session. Where no page cursor exists and a bare integer is given, the node
**MUST** respond in one line asking for the qualified form rather than guessing.

**REQ-CMD-018b** — Incident refs are the exception: `local_ref` (doc 05 §6) is a node-wide
short number, so `INC 31` is unambiguous whether or not a listing preceded it. Incident
listings therefore show `local_ref`, not a page-relative index.

---

## 5. Pagination

**REQ-CMD-019** — Any listing that exceeds its part budget **MUST** be paginated, and the
last part **MUST** end with a continuation affordance showing the remaining count:
`…MORE 7`.

**REQ-CMD-020** — The page cursor **MUST** be stored on the session and **MUST** survive for
`router.page_ttl_minutes` (default 15). An expired `MORE` returns
`Expired. Repeat the command.` in one line.

**REQ-CMD-021** — Default page sizes: boards 6, threads 5, posts 3, mail 5, incidents 5,
members 8. These are fixed rendering limits in the current release.

---

## 6. Drafts

**REQ-CMD-022** — When a pending compose action times out, its partial text **MUST** be
saved as a draft, and the member notified once (piggy-backed on their next interaction, not
as a separate transmission).

**REQ-CMD-023** — `POST` / `SEND` with no text while a draft exists **MUST** offer to resume
it in one line.

---

## 7. The terse register

**REQ-CMD-024** — All over-the-air text **MUST** follow these rules. They are normative,
not stylistic, and **MUST** be enforced by a lint test over the string catalogue
(doc 14 §5).

1. **Answer first.** The first 40 bytes carry the answer. Context, caveats, and affordances
   follow.
2. **No greetings, no sign-offs, no pleasantries.** Never "Sure!", "Here you go", "Hope that
   helps", "Let me know if…".
3. **No echo.** Never restate the user's input.
4. **Abbreviate by convention, not invention.** A fixed abbreviation table is maintained in
   `src/outpost/render/abbrev.py` and documented in `HELP ABBR`.
5. **Numbers over words.** `3 new` not `three new messages`.
6. **Drop units when unambiguous** in context (`72/48` for a high/low, `W12` for wind).
7. **24-hour local time**, `HH:MM`. Dates as `DD Mon` when within a year.
8. **One space after punctuation. No double spaces, no ASCII art, no box drawing.**
9. **Emoji only where they replace ≥4 bytes of text** and are in the approved set
   (`⚠` alert, `✓` ack, `📍` position). Never decorative.
10. **Errors are one line, imperative, and actionable.** `No board "rodes". Try: roads, wx,
    lost-found.`

**Reference renderings.** These are normative: an agent **MUST** match this density,
separator style, and page size. `·` is the field separator; runs of spaces are prohibited
(rule 8). Page sizes follow the fixed limits in REQ-CMD-021. Doc 07 §9 and doc 09 §6
extend this set and **MUST** stay consistent with it.

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

WHO
> 14 heard/24h
@dana 4m · @ray 22m · @jo 1h · @kit 3h · @sam 6h · @lee 8h · @mo 11h · @pat 19h
…MORE 6

WX
> Now 8C ovc W14g22 · Tonight rain 80% lo 5C · Tue 11C/3C shwrs
NWS 41m

ASK when does the transfer station open
> [AI] Sat 8-4, Wed 12-6. Closed otherwise. src: kb:transfer-station

REPORT tree down blocking cedar ln near the church
> ✓ INC 31 hazard · Cedar Ln · 📍44.123,-72.567 · sent to #watch
```

**REQ-CMD-025** — Rendered output **MUST** be validated in tests against a maximum byte
length per template. A template that cannot fit its worst-case data in its part budget is a
defect.

---

## 8. Natural-language tolerance

Users will not learn a command set. The router **MUST** meet them partway without spending
AI airtime on trivial cases.

**REQ-CMD-026** — Before falling through to the AI, the router **MUST** attempt, in order:

1. **Exact command / alias match.**
2. **Fuzzy match** on the first token — Levenshtein ≤2 for longer command-like tokens and ≤1 for
   tokens of four characters or fewer, disambiguated by trust level and enabled modules.
   `bords` → `BOARDS`. An unambiguous read-only match **MAY** execute directly. A match to a
   persistent, destructive, privacy, messaging, incident, alert, welfare, or operator mutation
   **MUST NOT** execute until the member selects an explicit numbered confirmation. An ambiguous
   match **MUST** offer bounded numbered choices without executing any candidate.
3. **Intent phrase table.** A curated, operator-extensible table of phrasings mapped to
   commands, evaluated as case-insensitive regex over the whole message. Ships with at
   minimum:

   | Pattern | Command |
   |---|---|
   | `what.*weather`, `is it going to rain`, `forecast` | `WX` |
   | `any(thing)? new`, `what.*happen(ing|ed)`, `catch me up` | `NEW` |
   | `who.*(around\|here\|online)` | `WHO` |
   | `help`, `what can you do`, `commands` | `HELP` |
   | `i'?m ok`, `checking in`, `all clear` | `OK` |
   | `where am i`, `my (position\|location)` | `POS` |

4. **AI fallback** (DM only, module enabled, channel permits).
5. **Terse unknown-command line** with the two most likely commands.

**REQ-CMD-027** — The intent table **MUST** be data, loaded from
`config/intents.yaml`, hot-reloadable, and testable. Operators extend it for local idiom.

**REQ-CMD-028** — Fuzzy and intent matches **MUST** be counted as metrics with the matched
command as a label, so operators can see what people are actually trying to do and extend
the table accordingly.

**REQ-CMD-028a** — Every built-in command and alias **MUST** remain globally reserved regardless
of module state. An exact token for a disabled or unavailable feature **MUST** receive a concise
feature-unavailable response and **MUST NOT** enter fuzzy matching. Command specifications **MUST**
classify mutations, and rejected mutation corrections, disabled commands, and ambiguities **MUST**
emit bounded, content-free telemetry.

---

## 9. Registration and first contact

**REQ-CMD-029** — The first ever message from an unknown member **MUST** be answered with a
single ≤200-byte orientation line, and **MUST NOT** demand registration before doing
something useful:

```
> New here. I'm CRO: boards, mail, alerts, wx. Your cmd ran: 3 boards, 1 alert.
Send NAME <handle> to be recognised. ? for help.
```

**REQ-CMD-030** — Handles **MUST** be 2–12 characters, `[a-z0-9_-]`, case-insensitively
unique per node, and **MUST NOT** collide with a command name or alias.

**REQ-CMD-031** — Claiming a handle already bound to a different mesh node ID **MUST** be
refused. Handle transfer requires operator action.

---

## 10. Command specification format

**REQ-CMD-032** — Every command **MUST** be declared as data, not as an ad-hoc `if` chain:

```python
@dataclass(frozen=True)
class CommandSpec:
    name: str
    aliases: tuple[str, ...]
    module: str
    args: tuple[ArgSpec, ...]
    min_trust: TrustLevel
    channels: ChannelPolicy          # dm_only | any | listed
    context_provides: tuple[str,...] # frames this command can draw defaults from
    context_pushes: str | None
    airtime_class: TrafficClass
    max_parts: int
    rate_key: str                    # bucket name for rate limiting
    help_short: str                  # ≤60 bytes
    help_long: str                   # ≤2 parts when rendered
    handler: Callable[[CommandCtx], Awaitable[Response]]
```

**REQ-CMD-033** — `HELP`, the dashboard's command reference, the AI's knowledge of available
commands, and the parser **MUST** all be generated from this registry. There **MUST NOT** be
a separately-maintained help text.

**REQ-CMD-034** — Argument parsing **MUST** be declarative (`ArgSpec` with type, optionality,
and a "rest of line" greedy variant) and **MUST** produce a terse, actionable usage line on
failure:
`POST needs text. Try: POST roads Bridge is open.`

---

## 11. Response model

**REQ-CMD-035** — Handlers **MUST** return a structured `Response`, never a string:

```python
@dataclass
class Response:
    kind: ResponseKind              # ack | listing | detail | error | none
    lines: list[Line]               # semantic lines; renderer decides layout
    page: PageCursor | None
    context_push: ContextFrame | None
    context_pop: bool
    airtime_class: TrafficClass
    broadcast: bool = False
    supersedes: str | None = None
    data: dict[str, Any] | None = None   # structured form for API/AI consumers
```

**REQ-CMD-036** — `ResponseKind.none` **MUST** be a valid, common outcome. Many inbound
messages deserve no transmission at all, and the system **MUST** make silence easy.

---

## 12. Anti-patterns

| ❌ | ✅ |
|---|---|
| `Welcome to the Cedar Ridge BBS! Please select an option:` | Answer the command they sent |
| Deep modal menu trees that make commands unavailable | Shallow discovery screens plus one-shot commands at every depth |
| `Are you sure? (Y/N)` for non-destructive actions | Just do it; provide `UNDO` where it matters |
| `Command not recognized. Type HELP for a list of commands.` | `Unknown. Did you mean BOARDS or BOARD?` |
| Reflowing the user's text back with quoting | Reference by number |
| Distinct help text maintained by hand | Generated from `CommandSpec` (REQ-CMD-033) |
| Sending help unprompted to new users before their command runs | Run their command, orient in the same message |
