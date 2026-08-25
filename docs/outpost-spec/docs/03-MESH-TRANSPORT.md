# 03 — Mesh Transport

**Status:** Baseline · **Prerequisite:** [02-ARCHITECTURE.md](02-ARCHITECTURE.md)
**Implements:** `src/outpost/transport/`

This document governs everything that touches the radio. It is the most constraint-dense
part of the system, and the place where a naive implementation will cause real-world harm
to a shared radio channel.

---

## 1. Protocol facts (verified)

An implementing agent **MUST** treat this table as authoritative and **MUST NOT** substitute
values recalled from training data. Several widely-circulated figures (237-byte payload,
"no duty-cycle enforcement") are stale.

| Fact | Value | Source |
|---|---|---|
| `Constants.DATA_PAYLOAD_LEN` | **233 bytes** | [mesh.proto](https://github.com/meshtastic/protobufs/blob/master/meshtastic/mesh.proto) |
| Practical text budget (client-composer cap) | **200 bytes** | [Messages & Channels](https://meshtastic.org/docs/software/android/user/messages-and-channels/) |
| PKI (DM) encryption overhead | 12 bytes → ~221 usable | `MESHTASTIC_PKC_OVERHEAD` in [RadioInterface.h](https://github.com/meshtastic/firmware/blob/master/src/mesh/RadioInterface.h) |
| Mesh header (outside the payload envelope) | 16 bytes | RadioInterface.h |
| Default hop limit / max | 3 / **7** (3-bit field) | [LoRa Config](https://meshtastic.org/docs/configuration/radio/lora/) |
| Default preset `LONG_FAST` | SF11, BW 250 kHz, CR 4/5, **1.07 kbps** | [Radio Settings](https://meshtastic.org/docs/overview/radio-settings/) |
| Channel slots | **8** (0–7); 0 is always primary | [Channel Config](https://meshtastic.org/docs/configuration/radio/channels/) |
| Default public channel PSK | 1 byte `0x01` → base64 `AQ==` — **publicly known** | [Channels.cpp](https://github.com/meshtastic/firmware/blob/master/src/mesh/Channels.cpp) |
| Firmware TX gate (hard) | Skips send above **40%** channel utilisation | [airtime.cpp](https://github.com/meshtastic/firmware/blob/master/src/airtime.cpp) |
| Firmware TX gate (polite) | **25%** channel utilisation | airtime.cpp |
| EU duty cycle | 10% region limit → effective **5%** air-util TX gate | airtime.cpp |
| Reliable retransmissions | **3** | [ReliableRouter.cpp](https://github.com/meshtastic/firmware/blob/master/src/mesh/ReliableRouter.cpp) |
| Broadcast ACK | **Implicit only** — hearing a rebroadcast. No per-recipient confirmation | [Mesh Broadcast Algorithm](https://meshtastic.org/docs/overview/mesh-algo/) |
| DM ACK | Explicit ACK returned to sender; means "delivered to the radio", not "read" | ReliableRouter.cpp |
| Max TX queue in firmware | 16 packets | RadioInterface.h |
| Private application portnums | **256–511** (`PRIVATE_APP = 256`) | [portnums.proto](https://github.com/meshtastic/protobufs/blob/master/meshtastic/portnums.proto) |

**Estimated airtime per packet** (computed from the Semtech ToA formula with firmware
parameters: preamble 16 symbols, explicit header, CRC on — *not quoted from an official
table; validate against the Meshtastic airtime calculator during Phase 0*):

| Preset | ~30-byte packet | 233-byte packet |
|---|---|---|
| `LONG_FAST` (SF11/250k) | ≈0.48 s | ≈2.1 s |
| `MEDIUM_SLOW` (SF10/250k) | ≈0.26 s | ≈1.16 s |
| `SHORT_FAST` (SF7/250k) | ≈0.04 s | ≈0.20 s |

**REQ-TRANSPORT-001** — The Governor's airtime accounting **MUST** use a per-preset
time-on-air model, not a byte count. The model **MUST** be a pure function
`toa(payload_bytes, preset) -> seconds`, unit-tested against the published preset data
rates, and **MUST** be recalibrated in Phase 0 against real measurements from
`airUtilTx` telemetry reported by the local radio.

---

## 2. Radio link

### 2.1 Transports

**REQ-TRANSPORT-002** — Three transports **MUST** be supported: `serial` (default), `tcp`,
`ble`. Selection is config-driven; the interface to the rest of the system is identical.

```python
class RadioLink(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def send_text(self, text: str, *, dest: str, channel: int,
                        want_ack: bool, priority: Priority) -> SendResult: ...
    async def send_data(self, payload: bytes, *, dest: str, channel: int,
                        portnum: int, want_ack: bool) -> SendResult: ...
    def inbound(self) -> AsyncIterator[InboundMessage]: ...
    async def node_db(self) -> Mapping[str, NodeRecord]: ...
    async def local_telemetry(self) -> LocalTelemetry: ...   # ch util, air util tx, battery
    @property
    def state(self) -> LinkState: ...                        # down|connecting|up|degraded
```

**REQ-TRANSPORT-003** — `serial` **MUST** be the documented default and the recommended
production transport. BLE **MUST** be marked "best-effort" in config comments and docs.

> **Why.** The Meshtastic Python `BLEInterface` carries an explicit source comment that on
> Linux the BLE device is not disconnected without an `atexit` hook and that "future
> connection attempts will fail" — the library's own words about BlueZ. For an unattended
> six-month deployment this is unacceptable as a primary path.
> ([ble_interface.py](https://python.meshtastic.org/ble_interface.html))

**REQ-TRANSPORT-004** — BLE reconnection **MUST** perform a full teardown of the Bleak
client and a fresh `BLEInterface.scan()` before retrying, not a bare reconnect. After 5
consecutive failures the node **MUST** log a `WARN` recommending serial and surface it on
the dashboard.

### 2.2 Bridging the synchronous library

The `meshtastic` package is synchronous and delivers messages via `pypubsub` callbacks on
its own thread.

**REQ-TRANSPORT-005** — Pubsub callbacks **MUST** do nothing but construct a raw record and
hand it to the event loop with `asyncio.run_coroutine_threadsafe` or
`loop.call_soon_threadsafe(queue.put_nowait, ...)`. No parsing, no I/O, no DB access in the
callback. A full inbound queue **MUST** drop the oldest non-alert item and count a metric,
never block the library thread.

**REQ-TRANSPORT-006** — The link **MUST** subscribe to the **catch-all** topic
`meshtastic.receive` and dispatch on the integer `portnum` field of the decoded packet,
plus `meshtastic.connection.established` and `meshtastic.connection.lost`.

> Do **not** subscribe to a per-portnum topic for the federation portnum. The
> `meshtastic` library derives `meshtastic.receive.<name>` topics from a table of *named*
> portnums; a number in the `PRIVATE_APP` range has no name, so a topic constructed from
> the integer would never fire and federation inbound would silently never work. The
> catch-all plus an integer switch is correct for both named and private portnums.

The named topics (`meshtastic.receive.text`, `.position`, `.user`, `.telemetry`) **MAY**
additionally be subscribed for clarity, but if they are, the catch-all handler **MUST**
skip those portnums to avoid double processing.

**REQ-TRANSPORT-007** — All `meshtastic` library calls (`sendText`, `sendData`, node DB
access) **MUST** be executed in a thread executor, never on the event loop.

### 2.3 Connection supervision

**REQ-TRANSPORT-008** — A supervisor task **MUST** monitor link liveness. The link is
considered `up` when a packet (of any kind, including the radio's own telemetry) has been
received within `liveness_timeout_s` (default 300) **or** a successful local-config read
has completed within 60 s. On timeout, force a reconnect cycle.

**REQ-TRANSPORT-009** — Reconnect backoff **MUST** be exponential with jitter:
`min(initial * 2^n, max)` with ±20% jitter; defaults `initial=2s`, `max=120s`. The
outbound queue is retained across reconnects, subject to per-class TTL (REQ-ARCH-021).

**REQ-TRANSPORT-010** — On connection establishment the node **MUST**: read local config
and record the region, modem preset, and configured channels; snapshot the node DB;
reconcile channel indices against `channels:` config and log an `ERROR` for any configured
channel index that does not exist on the radio.

---

## 3. Inbound pipeline

**REQ-TRANSPORT-011** — Every inbound packet **MUST** be normalised to:

```python
@dataclass(frozen=True)
class InboundMessage:
    packet_id: int
    from_id: str            # "!a1b2c3d4"
    to_id: str              # "!a1b2c3d4" or "^all"
    channel: int
    portnum: int
    is_direct: bool         # to_id == our node id
    text: str | None
    payload: bytes | None
    rx_time: datetime       # node clock, UTC
    rx_snr: float | None
    rx_rssi: int | None
    hops_away: int | None
    want_ack: bool
    pki_encrypted: bool
```

**REQ-TRANSPORT-012 (deduplication)** — An LRU of the last 2048 `(from_id, packet_id)`
pairs **MUST** be maintained; duplicates are dropped and counted. Meshtastic's managed
flooding means the same packet is frequently heard more than once.

**REQ-TRANSPORT-013 (loop protection)** — Messages whose `from_id` equals the node's own ID
**MUST** be discarded unconditionally, before any processing. This is the single most
common cause of runaway bot loops on a mesh.

**REQ-TRANSPORT-014 (bridge detection)** — The node **MUST** support an operator-configured
list of relay/bridge node IDs (Matrix, Discord, MQTT bridges) whose traffic is processed for
*reading* but never triggers a reply, to prevent bridge-echo loops.

**REQ-TRANSPORT-015 (unsolicited traffic)** — On the primary public channel the node
**MUST NOT** respond to messages that do not begin with an explicit invocation token
(see doc 04 §2), regardless of content. Keyword-triggered replies on the public channel are
prohibited, with exactly one exception: the emergency keyword set defined in doc 08 §4,
which is operator-configurable and disabled by default.

**REQ-TRANSPORT-016 (position ingest)** — `POSITION_APP` packets **MUST** update the
member's last-known position (`latitudeI`/`longitudeI` × 1e-7 → float degrees) with the
precision the sender chose. Position data is treated as sensitive; see doc 12 §8 for
retention, precision reduction, and consent.

**REQ-TRANSPORT-017 (clock)** — Inbound `rx_time` **MUST** be the node's own clock, not the
sender's claimed time. Sender-supplied timestamps are advisory only and **MUST NOT** be
used for ordering.

---

## 4. The Airtime Governor

> **This is the most important component in the system.** Everything that leaves the node
> passes through it. No module, handler, or debug path may call `RadioLink.send_*` directly.

### 4.1 Mandate

**REQ-TRANSPORT-018** — `AirtimeGovernor` **MUST** be the sole caller of `RadioLink.send_*`.
This **MUST** be enforced mechanically: `RadioLink` send methods are private to the
transport package and an import-lint rule (doc 14 §6) fails the build if any module outside
`transport/` references them.

### 4.2 Traffic classes

| Class | Purpose | Default share | TTL | Preemption |
|---|---|---|---|---|
The class set is **closed**. Every outbound item carries exactly one of these five.

| Class | Purpose | Default share | TTL | Preemption |
|---|---|---|---|---|
| `alert` | Life-safety alerts, escalations, all-clears | 0.30 | 24 h | Preempts everything |
| `reply` | Direct answer to a user request | 0.30 | 5 min | Normal |
| `ai` | Assistant responses | 0.15 | 3 min | Normal, yields to `reply` |
| `bulletin` | Operator announcements, presence beacon | 0.05 | 2 h | Low, deferred in quiet hours |
| `digest` | Subscription digests, scheduled summaries | 0.10 | 1 h | Low, deferred in quiet hours |
| `federation` | Node-to-node sync | 0.10 | 30 min | Lowest, never during elevated utilisation |

**REQ-TRANSPORT-019** — Class shares **MUST** be config-driven and **MUST** sum to ≤1.0.
Unused share from an idle class **MAY** be lent to a busier class, but `alert` **MUST**
always be able to reclaim its full share immediately by preempting queued work.

### 4.3 Budget enforcement

**REQ-TRANSPORT-020** — The Governor **MUST** maintain a rolling 60-minute window of the
node's own transmitted airtime (sum of `toa()` over sent packets) and **MUST NOT** exceed
`airtime.budget_percent` (default 8%) of wall-clock time within that window.

**REQ-TRANSPORT-020a (emergency reserve)** — A separate reserve of
`airtime.emergency_reserve_percent` (default **4%** of wall-clock time) sits *above* the
budget and is usable **only** by `alert`-class items of severity `critical`. The absolute
ceiling on the node's own airtime in any 60-minute window is therefore
`budget_percent + emergency_reserve_percent` (default 12%), and REQ-ARCH-017 rejects a
configuration where that sum reaches 20% or the utilisation ceiling.

The reserve exists so that a life-safety broadcast is never blocked by an hour of ordinary
traffic. It is bounded so that an escalation storm cannot saturate the channel and lock out
the responders it is trying to reach. Both properties are load-bearing; implement the
reserve in Phase 0 alongside the rest of the Governor, even though nothing uses it until
Phase 3.

**REQ-TRANSPORT-021** — The Governor **MUST** additionally read the radio's reported
`channelUtilization` and `airUtilTx` telemetry and **MUST** suspend all classes except
`alert` when channel utilisation exceeds `airtime.utilisation_ceiling` (default 25%).

> **Why 25%.** The firmware's own "polite" gate is 25% and its hard gate is 40%. A node that
> transmits up to the firmware's hard limit is, by definition, the node making the mesh
> unusable for everyone else. Outpost stops well before the firmware would stop it.

**REQ-TRANSPORT-022** — In regions with a duty cycle < 100% (EU 868/433), the Governor
**MUST** cap its own transmitted air-util at **2.5%** — half of the ~5% effective TX gate
the firmware already applies there (region `dutyCycle` 10% × `polite_duty_cycle_percent`
50%). The applied regional policy **MUST** be logged at startup with the computed number,
so an operator can see exactly what ceiling is in force.

`override_duty_cycle` **MUST NOT** be set by Outpost under any circumstance.

**REQ-TRANSPORT-022a** — In a duty-cycle-limited region, `airtime.budget_percent` and
`airtime.emergency_reserve_percent` **MUST** be clamped so their sum does not exceed 2.5%,
and startup **MUST** log a warning naming the configured values and the clamped values.

**REQ-TRANSPORT-023 (quiet hours)** — Classes listed in `airtime.quiet_hours.classes`
**MUST** be deferred (not dropped) during the configured window, and released afterwards
subject to TTL. `alert` **MUST NOT** be subject to quiet hours.

**REQ-TRANSPORT-024 (pacing)** — Consecutive transmissions **MUST** be separated by a
minimum inter-packet gap, default `max(2.0s, 4 × toa(previous_packet))`. Multi-part
responses use the longer gap defined in §5.

### 4.4 Scheduling algorithm

**REQ-TRANSPORT-025** — Scheduling **MUST** be deficit round-robin over class queues,
weighted by class share, with strict priority for `alert`:

```
loop every tick (250 ms):
    if link is not up:                          continue
    if measured_ch_util > ceiling:              serve only `alert`
    if window_airtime_used >= budget:           serve only `critical` alerts, drawing on the
                                                emergency reserve; count throttle metric
    if window_airtime_used >= budget + reserve: serve nothing; count a hard-stop metric
    if now < next_allowed_tx_at:                continue
    pick class:
        if alert queue non-empty:               alert
        else:                                   deficit-round-robin over remaining classes,
                                                skipping classes over their share and
                                                classes deferred by quiet hours
    pop item; drop if past TTL (count metric)
    transmit; record toa into the rolling window
    next_allowed_tx_at = now + gap(item)
```

**REQ-TRANSPORT-026** — Every drop, defer, and throttle **MUST** emit a counted metric with
the class as a label, and **MUST** be visible on the dashboard. Silent shedding is a defect:
the operator must be able to see that the node is choosing not to talk.

### 4.5 Coalescing and suppression

**REQ-TRANSPORT-027** — Before enqueue, the Governor **MUST** apply:

- **Duplicate suppression.** Identical `(dest, channel, text_hash)` already queued or sent
  within `dedupe_window_s` (default 300) is dropped.
- **Supersession.** A newer item may declare `supersedes=<queue_key>`; the older item is
  removed. Used for alert updates and for regenerated digests.
- **Coalescing.** Items to the same destination in the same class within
  `coalesce_window_s` (default 15) **MAY** be merged if the renderer supplies a merge
  function and the merged result still fits the part budget.

### 4.6 Broadcast discipline

**REQ-TRANSPORT-028** — Broadcasts are strictly limited. The node **MUST NOT** broadcast
except for: `alert` class content, operator-initiated bulletins, and the once-per-interval
presence beacon (§7). Everything else is a direct message to the requester.

**REQ-TRANSPORT-029** — Broadcast rate **MUST** be capped independently of the airtime
budget: default no more than 6 broadcasts per hour outside `alert` class, and no more than
1 per 60 s within `alert` class for the same incident.

---

## 5. Response chunking

**REQ-TRANSPORT-030** — The chunker **MUST** target **200 bytes of UTF-8** per part, with a
hard ceiling of 233 bytes minus PKI overhead when the destination is PKI-capable. It
**MUST** measure encoded bytes, not characters.

**REQ-TRANSPORT-031** — Splitting **MUST** occur on, in order of preference: an explicit
part marker inserted by the renderer, a sentence boundary, a list-item boundary, a word
boundary. Splitting mid-word or mid-multibyte-sequence is prohibited.

**REQ-TRANSPORT-032** — Multi-part responses **MUST** carry a part indicator as a **suffix**
in the form ` (1/3)`, counted against the 200-byte budget. A suffix is used rather than a
prefix so the meaningful text appears first on a truncating device screen.

**REQ-TRANSPORT-033** — Part budgets **MUST** be enforced per class, config-driven:

| Class | Default max parts |
|---|---|
| `reply` | 3 |
| `ai` | **2** |
| `bulletin` | 2 |
| `digest` | 4 |
| `alert` | 2 |
| `federation` | n/a (binary framing, §6) |

For class `ai` the 2-part budget is the transport ceiling; the *renderer* additionally
targets a single part, and the model is instructed to a tighter limit still (doc 06 §6.1).
The second part exists as headroom, not as an expectation.

When a rendered response exceeds its part budget, the renderer **MUST** truncate and append
a continuation affordance (e.g. `…MORE 4` — see doc 04 §5), never emit extra parts.

**REQ-TRANSPORT-034** — Inter-part delay **MUST** default to 12 s and be config-driven.
LoRa gives no ordering guarantee; parts sent back to back frequently arrive out of order or
collide.

> Community precedent: the `Meshbot_weather` project uses a 15 s inter-message delay for
> exactly this reason. 12 s is chosen as a floor; operators on congested meshes should
> raise it.

**REQ-TRANSPORT-035** — Multi-part responses **MUST** be atomic in the queue: either all
parts are enqueued or none. If the budget will not permit all parts, the response is
truncated to what fits before enqueue.

---

## 6. Federation framing

**REQ-TRANSPORT-036** — Node-to-node traffic **MUST** use a dedicated portnum in the
private range (256–511), default **260**, config-driven as `radio.federation_portnum`.

> The private range is unmanaged — no registry prevents another project from choosing the
> same number. The framing therefore begins with a magic value so foreign traffic on the
> same portnum is discarded rather than misparsed.

**REQ-TRANSPORT-037** — Frame layout on `sendData`:

```
byte 0       magic         0x4F   ('O')
byte 1       version       0x01
byte 2       msg_type      see doc 10 §3
byte 3       flags         bit0 = more-fragments
                           bit1 = body is deflate-compressed
                           bits2-7 reserved (MUST be 0)
bytes 4-5    fragment      uint8 index, uint8 total
bytes 6-9    counter       uint32 BE, monotonic per (sender, peer) — replay protection
bytes 10-17  hmac          first 8 bytes of HMAC-SHA256 over bytes 0-9 ‖ body,
                           keyed by the peer's shared secret
bytes 18..   body          CBOR
```

Maximum body: **`233 - 18 = 215` bytes per fragment.** With `fed.max_fragments` = 8 the
ceiling on a single logical message is **1720 bytes**.

**REQ-TRANSPORT-036a** — The receiver **MUST** reject a frame whose `counter` is less than
or equal to the highest counter already accepted from that peer, and **MUST** count it as a
replay. Counters are persisted per peer so a restart does not reopen the window.

**REQ-TRANSPORT-038** — Federation frames **MUST** be sent with `want_ack=True` to a
specific destination node, never broadcast, except for the peer-discovery beacon
(doc 10 §4).

**REQ-TRANSPORT-039** — Unparseable, wrong-magic, or wrong-version frames **MUST** be
silently discarded and counted. No error is transmitted — a foreign application using the
same portnum must not be replied to.

---

## 7. Presence beacon

**REQ-TRANSPORT-040** — The node **MAY** broadcast a presence beacon on its configured
Outpost channel at airtime class `bulletin`, default every 6 hours, disabled on the primary
public channel by default. Content is one line ≤120 bytes: node short name, a one-word
invitation, and the invocation token. Example:

```
CRO Outpost: boards, mail, wx. Send "?" to start.
```

**REQ-TRANSPORT-041** — The beacon **MUST** be suppressed when channel utilisation exceeds
15%, during quiet hours, and when the node has served at least one command in the preceding
hour (the community already knows it exists).

---

## 8. Acknowledgement handling

**REQ-TRANSPORT-042** — Direct messages carrying `reply` or `alert` class content **MUST**
be sent with `want_ack=True`. Broadcasts **MUST** be sent with `want_ack=False` — requesting
ACKs on a broadcast generates an ACK storm.

**REQ-TRANSPORT-043** — ACK/NAK results **MUST** be recorded on the `message_log` row with
outcome `acked | naked | timeout | not_requested`. The node **MUST NOT** retransmit on NAK
at the application layer; the firmware already retries 3 times, and application-layer retry
on top of that multiplies airtime.

**REQ-TRANSPORT-044** — An ACK means "delivered to the destination radio", not "read by a
person". No UI, over-air text, or API field may describe it as read confirmation.

**REQ-TRANSPORT-045** — Repeated NAK/timeout to the same destination (default 5 consecutive)
**MUST** mark that member `unreachable`, suppress queued non-alert traffic to them, and
resume on the next inbound message from that member.

---

## 9. Metrics

**REQ-TRANSPORT-046** — The transport layer **MUST** export at minimum:

```
outpost_radio_link_state                       gauge  {state}
outpost_radio_reconnects_total                 counter
outpost_inbound_messages_total                 counter {portnum,channel,direct}
outpost_inbound_dropped_total                  counter {reason}
outpost_outbound_enqueued_total                counter {class}
outpost_outbound_sent_total                    counter {class,dest_type}
outpost_outbound_dropped_total                 counter {class,reason}
outpost_outbound_queue_depth                   gauge   {class}
outpost_airtime_used_ratio                     gauge            # own, rolling 1h
outpost_channel_utilisation_ratio              gauge            # radio-reported
outpost_air_util_tx_ratio                      gauge            # radio-reported
outpost_governor_throttled_seconds_total       counter {class}
outpost_response_parts                         histogram {class}
outpost_ack_outcome_total                      counter {outcome}
outpost_toa_seconds                            histogram
```

---

## 10. Testability

**REQ-TRANSPORT-047** — `RadioLink` **MUST** have a `SimulatedRadioLink` implementation used
by the test suite, supporting: configurable per-link loss rate, latency distribution,
duplicate delivery, out-of-order delivery, ACK/NAK injection, channel-utilisation
simulation, and a virtual clock. See [14-TESTING.md](14-TESTING.md) §3.

**REQ-TRANSPORT-048** — The Governor **MUST** be testable against a virtual clock with no
real sleeping. All timing **MUST** flow through an injected `Clock` protocol.

**REQ-TRANSPORT-049** — There **MUST** be a property-based test asserting this invariant,
stated once here and referenced everywhere else (doc 08 §6, doc 14 T-01):

> For any sequence of enqueue operations and any simulated channel conditions:
> 1. Total transmitted airtime in any 60-minute window never exceeds
>    `budget_percent + emergency_reserve_percent`.
> 2. Airtime attributable to non-`alert` classes never exceeds `budget_percent`.
> 3. Airtime drawn from the emergency reserve is attributable **only** to `alert`-class
>    items of severity `critical`.
> 4. Each non-`alert` class's airtime never exceeds its own share of `budget_percent`,
>    except where it borrowed share from a class that was idle for the whole window.

---

## 11. Anti-patterns

Explicitly prohibited. Each of these has been observed causing harm on live meshes.

| ❌ Anti-pattern | ✅ Instead |
|---|---|
| Replying to every message on the public channel | Explicit invocation token required (REQ-TRANSPORT-015) |
| Sending an "OK, working on it…" acknowledgement | Answer once, or not at all |
| Application-layer retry after a NAK | Trust the firmware's 3 retries (REQ-TRANSPORT-043) |
| Broadcasting a reply to a question one person asked | DM the asker (REQ-TRANSPORT-028) |
| Streaming a 6-part AI answer | 2-part budget with a `…MORE` affordance (REQ-TRANSPORT-033) |
| Periodic "still alive" broadcasts | Suppressed beacon (REQ-TRANSPORT-041) |
| Echoing the user's input back in the reply | Airtime spent restating what they already know |
| Emitting a stack trace or exception text | One generic line (REQ-ARCH-012) |
| Setting `override_duty_cycle` | Prohibited (REQ-TRANSPORT-022) |
| Sending `want_ack` on broadcasts | ACK storm (REQ-TRANSPORT-042) |
