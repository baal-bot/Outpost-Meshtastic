"""Live challenges through production authentication, queue, and clock policy."""

import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

from outpost.fed.framing import MessageType
from outpost.fed.time import FederationTime
from outpost.timekeeping import (
    HOLDOVER_SECONDS,
    PeerTimeEvidence,
    TimeSource,
    TimeStatus,
    time_status,
)
from outpost.transport.governor import OutboundItem
from outpost.transport.models import InboundMessage, TrafficClass
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_time_confidence import monitor
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


async def pair(nodes, monkeypatch, *, cold=False, offset=0):
    local, _, _ = await nodes()
    remote, _, _ = await nodes("remote")
    for app in (local, remote):
        await app.database.write(
            "UPDATE fed_peer SET capabilities=?,policy_configured=1", ('{"time_v1":true}',)
        )
    local.clock.epoch += timedelta(seconds=offset)
    monitor(monkeypatch, local.clock, synchronized=not cold)
    source = monitor(monkeypatch, remote.clock)
    if cold:
        local.governor._time_started_safe = False
    await local.governor.recover()
    await remote.governor.recover()
    await local.federation_time.configure("!remote", trust=True, serve=False, actor="test")
    await remote.federation_time.configure("!local", trust=False, serve=True, actor="test")
    return local, remote, source


async def transmit(sender, receiver):
    item = await sender.governor.tick()
    assert item is not None
    assert item.binary_payload is not None and len(item.binary_payload) <= 188
    await receiver._handle_federation_discovery(
        InboundMessage(
            from_id=sender.radio.local_node_id,
            payload=item.binary_payload,
            portnum=sender.config.radio.federation_portnum,
            packet_id=item.item_id,
            to_id="^all",
            channel=0,
            is_direct=False,
            text=None,
            rx_time=receiver.clock.now(),
        )
    )
    return item


async def exchange(local, remote, *, delay=2):
    assert (await local.federation_time.request("!remote"))["state"] == "waiting"
    await transmit(local, remote)
    for app in (local, remote):
        app.clock.advance(delay)
    return await transmit(remote, local)


def advance(nodes, seconds):
    for app in nodes:
        app.clock.advance(seconds)


@pytest.mark.parametrize("offset", [0, -21600, 21600])
async def test_cold_boot_probe_preserves_work_and_only_validates_actual_utc(
    nodes, monkeypatch, offset
):
    local, remote, _ = await pair(nodes, monkeypatch, cold=True, offset=offset)
    with pytest.raises(ValueError, match="silent"):
        await local.federation_time.request("!remote")
    ordinary = OutboundItem("retained", "^all", 0, TrafficClass.FEDERATION)
    assert await local.governor.admit(ordinary) is None
    assert await local.governor.tick() is None
    advance((local, remote), 3601)
    response = await exchange(local, remote)
    status = time_status(local.clock)
    assert status.timestamp_safe is (offset == 0)
    assert status.source == "federation_peer"
    assert abs(status.peer_offset_seconds + offset) <= 2
    assert not status.holdover_verified
    assert local.governor.used_airtime > 0
    assert not await local.database.read("SELECT * FROM fed_inbox_item")
    assert not await local.database.read("SELECT * FROM fed_relay_envelope")
    if offset == 0:
        await local.governor.tick()
        assert not local.governor._time_recovery_pending
        assert local.governor.used_airtime > 0
    # Even a new authenticated counter cannot make a used nonce fresh again.
    secret = await remote.federation.secret("!local")
    fragment = remote.federation_codec.decode_fragment(response.binary_payload, secret)
    value = remote.federation_reassembler.add("!remote", fragment)
    with pytest.raises(ValueError, match="live matching"):
        await local.federation_time.receive(
            "!remote", MessageType.TIME_RESPONSE, value, hashlib.sha256(secret).digest()
        )


async def test_powered_multi_day_fallback_refreshes_without_exporting_peer_time(nodes, monkeypatch):
    local, remote, _ = await pair(nodes, monkeypatch)
    monitor(monkeypatch, local.clock, synchronized=False)
    for _ in range(3):
        advance((local, remote), 86400)
        assert not time_status(local.clock).timestamp_safe
        await exchange(local, remote)
        assert time_status(local.clock).timestamp_safe
        assert local.federation_time._native() is None
    # A six-hour wall jump invalidates otherwise fresh evidence immediately.
    local.clock.epoch += timedelta(hours=6)
    assert not time_status(local.clock).timestamp_safe
    assert not local.federation_time.evidence.references


@pytest.mark.parametrize("action", ["timeout", "revoke", "replace", "disable", "tamper"])
async def test_pending_probes_cannot_bypass_freshness_or_policy(nodes, monkeypatch, action):
    local, remote, _ = await pair(nodes, monkeypatch)
    monitor(monkeypatch, local.clock, synchronized=False)
    await local.federation_time.request("!remote")
    queued = local.governor.queues[TrafficClass.FEDERATION][0]
    if action == "timeout":
        advance((local, remote), 31)
        await local.federation_time.tick()
    elif action == "revoke":
        await local.federation.set_state("!remote", "pending")
    elif action == "replace":
        await local.federation.create_pairing_request("!remote", replace=True)
    elif action == "disable":
        local.config.modules.fed.enabled = False
    else:
        queued.binary_payload += b"x"
    assert await local.governor.tick() is None
    assert not local.radio.sent
    assert not time_status(local.clock).timestamp_safe
    assert not local.federation_time.owns(replace(queued, traffic_class=TrafficClass.ALERT))
    advance((local, remote), 31)
    await local.governor.tick()
    assert not local.governor.queues[TrafficClass.FEDERATION]


async def test_inflight_response_requires_current_approval_and_fresh_nonce(nodes, monkeypatch):
    local, remote, _ = await pair(nodes, monkeypatch)
    monitor(monkeypatch, local.clock, synchronized=False)
    await local.federation_time.request("!remote")
    await transmit(local, remote)
    await local.federation_time.configure("!remote", trust=False, serve=False, actor="test")
    await transmit(remote, local)
    assert not time_status(local.clock).timestamp_safe
    assert not local.federation_time.evidence.references


async def test_peer_derived_or_unsynchronized_sources_do_not_serve(nodes, monkeypatch):
    local, remote, source = await pair(nodes, monkeypatch)
    source[0] = TimeSource(False)
    advance((local, remote), HOLDOVER_SECONDS + 1)
    await local.federation_time.request("!remote")
    await transmit(local, remote)
    assert not remote.governor.queues[TrafficClass.FEDERATION]
    assert not remote.radio.sent


async def test_restart_keeps_airtime_cost_without_trusting_previous_boot_utc(nodes, monkeypatch):
    local, remote, _ = await pair(nodes, monkeypatch)
    await local.federation_time.request("!remote")
    await transmit(local, remote)
    assert await local.database.read(
        "SELECT * FROM runtime_setting WHERE key='airtime.time_recovery'"
    )
    # A new governor charges reserved costs without blocking unrelated traffic
    # for an entire hour when usable UTC can reconstruct the ordinary history.
    restarted = production_governor(local.database, local.clock, link=local.radio)
    await restarted.recover()
    assert restarted.time_recovery_wait == 0
    assert restarted.used_airtime >= local.governor.used_airtime
    before = len(local.radio.sent)
    assert await restarted.tick() is None
    assert len(local.radio.sent) == before
    alert = OutboundItem("restart alert", "^all", 0, TrafficClass.ALERT)
    assert await restarted.admit(alert)
    local.clock.advance(120)
    assert (await restarted.tick()).text == "restart alert"
    local.clock.advance(3601)
    await restarted.tick()
    assert not await local.database.read(
        "SELECT * FROM runtime_setting WHERE key='airtime.time_recovery'"
    )


@pytest.mark.parametrize("value", ["true", "not-json", "[-1]", "[4000]", "[true]"])
async def test_invalid_recovery_costs_require_a_silent_hour(nodes, monkeypatch, value):
    local, _, _ = await pair(nodes, monkeypatch)
    await local.database.write(
        "INSERT INTO runtime_setting(key,value,updated_at) VALUES('airtime.time_recovery',?,0)",
        (value,),
    )
    restarted = production_governor(local.database, local.clock, link=local.radio)
    await restarted.recover()
    assert restarted.time_recovery_wait == 3600
    assert await restarted.tick() is None


async def test_federation_limits_apply_to_recovery_frames(nodes, monkeypatch):
    local, remote, _ = await pair(nodes, monkeypatch)
    monitor(monkeypatch, local.clock, synchronized=False)
    await local.federation_time.request("!remote")
    local.governor.config.class_shares["federation"] = 0
    assert await local.governor.tick() is None
    assert not local.radio.sent
    with pytest.raises(ValueError, match="rate limited"):
        await local.federation_time.request("!remote")
    local.governor.config.class_shares["federation"] = 0.2
    local.governor.sync_radio_profile("LONG_FAST", "UNKNOWN")
    assert await local.governor.tick() is None
    assert not local.radio.sent


async def test_policy_load_and_key_binding_and_old_peer_compatibility(nodes, monkeypatch):
    local, remote, _ = await pair(nodes, monkeypatch)
    old = local.federation_time
    broker = FederationTime(
        local.federation,
        local.clock,
        local.governor,
        local.federation_codec,
        old.identity,
        old.channel,
        old.port,
        old.enabled,
    )
    await broker.load()
    assert broker.policies["!remote"].trust
    assert not broker.pending and not broker.evidence.references
    await local.database.write("UPDATE fed_peer SET capabilities='{}'")
    with pytest.raises(ValueError, match="advertised"):
        await broker.request("!remote")
    await local.database.write("UPDATE fed_peer SET shared_secret=?", (b"replacement" * 3,))
    with pytest.raises(ValueError, match="earlier pairing"):
        await broker._credential("!remote")
    await broker.tick()
    assert not broker.evidence.references


def test_conflicting_peers_and_source_age_are_conservative():
    evidence = PeerTimeEvidence()
    native = TimeStatus("uncertain", "no_source", False)
    wall = 1_800_000_000
    for peer, offset in (("a", 0), ("b", 4)):
        evidence.observe(
            peer,
            utc=wall + offset,
            error=0.1,
            age=HOLDOVER_SECONDS - 5,
            started=0,
            received=1,
            wall=wall,
        )
    assert evidence.sample(native, wall, 1).reason == "peer_sources_disagree"
    evidence.revoke("b")
    assert evidence.sample(native, wall + 1, 2).timestamp_safe
    assert not evidence.sample(native, wall + 5, 6).timestamp_safe
    assert not evidence.references


@pytest.mark.parametrize(
    "field,value", [("error", float("nan")), ("age", -1), ("received", 31), ("utc", 1)]
)
def test_bad_samples_cannot_grant_confidence(field, value):
    evidence = PeerTimeEvidence()
    sample = dict(utc=1_800_000_000, error=0.1, age=0, started=0, received=1, wall=1_800_000_000)
    sample[field] = value
    with pytest.raises(ValueError):
        evidence.observe("a", **sample)


async def test_unauthorized_pairing_request_cannot_revoke_a_valid_clock_reference(
    nodes, monkeypatch
):
    local, remote, _ = await pair(nodes, monkeypatch)
    monitor(monkeypatch, local.clock, synchronized=False)
    await exchange(local, remote)
    assert time_status(local.clock).timestamp_safe
    with pytest.raises(ValueError, match="operator-authorized"):
        await local.federation.accept_pairing_request("!remote", bytes(range(32)), bytes(16))
    assert time_status(local.clock).timestamp_safe


async def test_revocation_during_credential_read_cannot_install_a_reference(nodes, monkeypatch):
    local, remote, _ = await pair(nodes, monkeypatch)
    monitor(monkeypatch, local.clock, synchronized=False)
    await local.federation_time.request("!remote")
    await transmit(local, remote)
    original = local.federation_time._credential

    async def revoke_after_read(peer, tx=None):
        value = await original(peer, tx)
        await local.federation.set_state(peer, "pending")
        return value

    monkeypatch.setattr(local.federation_time, "_credential", revoke_after_read)
    await transmit(remote, local)
    assert not time_status(local.clock).timestamp_safe
    assert not local.federation_time.evidence.references


async def test_cold_recovery_control_does_not_mutate_or_release_retained_alert(nodes, monkeypatch):
    local, remote, _ = await pair(nodes, monkeypatch)
    alert = OutboundItem("retained alert", "^all", 0, TrafficClass.ALERT)
    assert await local.governor.admit(alert)
    before = dict(
        (await local.database.read("SELECT * FROM outbound_work WHERE id=?", (alert.item_id,)))[0]
    )
    monitor(monkeypatch, local.clock, synchronized=False)
    old = local.federation_time
    local.governor = production_governor(local.database, local.clock, link=local.radio)
    local.federation_time = FederationTime(
        local.federation,
        local.clock,
        local.governor,
        local.federation_codec,
        old.identity,
        old.channel,
        old.port,
        old.enabled,
    )
    await local.governor.recover()
    advance((local, remote), 3601)
    await local.federation_time.request("!remote")
    await transmit(local, remote)
    assert (
        dict(
            (await local.database.read("SELECT * FROM outbound_work WHERE id=?", (alert.item_id,)))[
                0
            ]
        )
        == before
    )
    assert len(local.radio.sent) == 1 and local.radio.sent[0].payload
    await transmit(remote, local)
    assert time_status(local.clock).timestamp_safe
    local.clock.advance(60)
    sent = await local.governor.tick()
    assert sent is not None and sent.text == "retained alert"


async def test_delayed_reply_and_unsupported_source_do_not_reset_holdover(nodes, monkeypatch):
    local, remote, _ = await pair(nodes, monkeypatch)
    monitor(monkeypatch, local.clock, synchronized=False)
    await local.federation_time.request("!remote")
    pending = local.federation_time.pending["!remote"]
    value = {
        "mesh_id": "!remote",
        "target_mesh_id": "!local",
        "n": pending.nonce,
        "u": round(remote.clock.now().timestamp() * 1000),
        "e": 1,
        "a": 0,
        "s": "peer",
    }
    credential = hashlib.sha256(await remote.federation.secret("!local")).digest()
    with pytest.raises(ValueError, match="native kernel"):
        await local.federation_time.receive("!remote", MessageType.TIME_RESPONSE, value, credential)
    value["s"] = "kernel"
    with pytest.raises(ValueError, match="credential changed"):
        await local.federation_time.receive("!remote", MessageType.TIME_RESPONSE, value, bytes(32))
    advance((local, remote), 31)
    with pytest.raises(ValueError, match="live matching"):
        await local.federation_time.receive("!remote", MessageType.TIME_RESPONSE, value, credential)
    assert not time_status(local.clock).timestamp_safe
    assert local.federation_time.results["!remote"]["state"] == "timeout"


async def test_governor_delay_consumes_the_live_challenge_budget(nodes, monkeypatch):
    local, remote, _ = await pair(nodes, monkeypatch)
    monitor(monkeypatch, local.clock, synchronized=False)
    await local.federation_time.request("!remote")
    advance((local, remote), 28)
    await transmit(local, remote)
    advance((local, remote), 3)
    await transmit(remote, local)
    assert not time_status(local.clock).timestamp_safe
    assert local.federation_time.results["!remote"]["state"] == "timeout"
