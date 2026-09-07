"""Lossless, opt-in incident encoding through the existing guarded radio path."""

import asyncio
import copy
import json

import pytest

from outpost.fed.framing import MessageType, Reassembler
from outpost.fed.incident_events import (
    COMPACT_CAPABILITY,
    PAYLOAD_FIELDS,
    compact_payload,
    content_digest,
    expand_payload,
)
from tests.integration.test_federation_incident_events import prepare as receive_fixture
from tests.integration.test_federation_incident_notes import source_note
from tests.integration.test_federation_item_failures import flush_radio, wire
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_incident_sender import prepare, saved

pytestmark = pytest.mark.production_wiring


async def capability(app, peer, version=1):
    current = await app.federation.by_mesh_id(peer.mesh_id)
    await app.database.write(
        "UPDATE fed_peer SET capabilities=? WHERE id=?",
        (json.dumps({**current.capabilities, COMPACT_CAPABILITY: version}), peer.id),
    )
    return await app.federation.by_mesh_id(peer.mesh_id)


def decoded(app, *, unsigned=False):
    assembler = Reassembler()
    values = []
    for packet in app.radio.sent:
        fragment = app.federation_codec.decode_fragment(
            packet.payload, None if unsigned else bytes(range(32))
        )
        value = assembler.add(app.radio.local_node_id, fragment)
        if value is not None:
            values.append(value)
    return values


def test_wire_codes_are_stable_and_every_value_survives_exactly():
    assert PAYLOAD_FIELDS == (
        "uid",
        "type",
        "severity",
        "status",
        "title",
        "body",
        "lat",
        "lon",
        "location_text",
        "radius_m",
        "reporter_label",
        "origin_node",
        "created_at",
        "updated_at",
        "expires_at",
        "resolved_at",
        "resolution_note",
        "origin_uids",
        "incident_uid",
        "kind",
        "author_label",
    )
    payload = dict.fromkeys(PAYLOAD_FIELDS)
    payload.update(
        title="Café — 水 🚧",
        body="Do not truncate " * 300,
        lat=40.4406123456789,
        lon=-79.9959123456789,
        created_at=1788801346,
        origin_uids=["!source:a", "!source:b"],
        future_extension={"status": "advisory", "values": [None, True, 1.5]},
    )
    compact = compact_payload(payload)
    assert set(compact) == {*range(21), "future_extension"}
    restored = expand_payload(compact)
    assert restored == payload and content_digest(restored) == content_digest(payload)


@pytest.mark.parametrize(
    "payload",
    [None, [], {True: "x"}, {1.0: "x"}, {-1: "x"}, {21: "x"}, {0: "a", "uid": "b"}, {b"uid": "x"}],
)
def test_malformed_or_aliasing_compact_fields_are_rejected(payload):
    with pytest.raises(ValueError, match="compact"):
        expand_payload(payload)


def test_non_string_source_fields_are_not_coerced():
    with pytest.raises(ValueError, match="field names"):
        compact_payload({1: "not a valid export"})


@pytest.mark.parametrize("version", [1, None, True, "1", 1.0, 2])
async def test_negotiation_preserves_legacy_and_exact_storage_without_human_import(nodes, version):
    source, sp, target, tp, incident = await prepare(nodes)
    sp, tp = await capability(source, sp, version), await capability(target, tp, version)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    await flush_radio(source, target)
    value = decoded(source)[0]
    compact = type(version) is int and version == 1
    assert value["mode"] == (2 if compact else 1)
    event = value["event"]
    payload = expand_payload(event["payload"]) if compact else event["payload"]
    inbox = (await target.database.read("SELECT * FROM fed_inbox_item"))[0]
    assert json.loads(inbox["payload_json"]) == payload and inbox["state"] == "pending"
    assert not await target.database.read("SELECT * FROM incident")
    assert not await target.database.read("SELECT * FROM alert")
    await flush_radio(target, source)
    receipt = decoded(target)[0]
    assert receipt["mode"] == 1 and receipt["digest"] == content_digest(payload)
    assert (await saved(source))[0]["stored_at"] is not None


async def test_compact_notes_keep_exact_parent_receipt_requirement(nodes):
    source, sp, target, tp, incident = await prepare(nodes, notes=True)
    sp, tp = await capability(source, sp), await capability(target, tp)
    _, note = await source_note(source, sp, incident, "Café crew: bridge remains closed.")
    uid = source.federation_sync._local_uid(note["uid"])
    await source.federation_sync.incident_handoff.stage(sp.id)
    with pytest.raises(ValueError, match="parent storage"):
        await source.incident_sender.admit(sp.id, "incident_updates", uid)
    await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    await flush_radio(source, target)
    await flush_radio(target, source)
    await source.incident_sender.admit(sp.id, "incident_updates", uid)
    await flush_radio(source, target)
    inbox = (
        await target.database.read(
            "SELECT payload_json,state FROM fed_inbox_item WHERE stream='incident_updates'"
        )
    )[0]
    assert json.loads(inbox[0]) == note["payload"] and inbox[1] == "pending"
    assert not await target.database.read("SELECT * FROM incident_update")


async def test_hello_advertises_compact_only_when_watch_is_enabled(nodes):
    app, _, _ = await nodes()
    await app._queue_federation_hello("^all")
    for _ in range(10):
        await app.governor.tick()
        app.clock.advance(12)
    assert decoded(app, unsigned=True)[0]["capabilities"][COMPACT_CAPABILITY] == 1
    app.config.modules.watch.enabled = False
    await app._queue_federation_hello("^all")
    for _ in range(10):
        await app.governor.tick()
        app.clock.advance(12)
    assert COMPACT_CAPABILITY not in decoded(app, unsigned=True)[-1]["capabilities"]


@pytest.mark.parametrize("upgrade", [False, True])
async def test_capability_change_before_dispatch_is_safe_in_both_directions(nodes, upgrade):
    source, sp, target, tp, incident = await prepare(nodes)
    if not upgrade:
        sp, tp = await capability(source, sp), await capability(target, tp)
    admission = await source.incident_sender.admit(sp.id, "incidents", incident.uid)
    await capability(source, sp, 1 if upgrade else None)
    if upgrade:
        await capability(target, tp)
        await flush_radio(source, target)
        assert decoded(source)[0]["mode"] == 1
        assert await target.database.read("SELECT * FROM fed_inbox_item")
    else:
        await source.governor.tick()
        assert not source.radio.sent
        assert not await source.database.read("SELECT * FROM outbound_attempt")
        work = await source.database.read(
            "SELECT state,last_error FROM outbound_work WHERE id=?", (admission.frame_ids[0],)
        )
        assert work[0][0] == "failed" and work[0][1] == "dispatch authorization denied"


async def test_receiver_rechecks_compact_capability_after_writer_wait(nodes):
    _, _, target, tp, _, event = await receive_fixture(nodes)
    tp = await capability(target, tp)
    value = {
        "mode": 2,
        "mesh_id": tp.mesh_id,
        "target_mesh_id": "!local",
        "event": {**event, "payload": compact_payload(event["payload"])},
    }
    async with target.database.transaction() as tx:
        task = asyncio.create_task(
            target.federation_sync.incident_events.receive_wire(tp, value, 100)
        )
        await asyncio.sleep(0)
        await tx.write(
            "UPDATE fed_peer SET capabilities=? WHERE id=?",
            (json.dumps({**tp.capabilities, COMPACT_CAPABILITY: 0}), tp.id),
        )
    with pytest.raises(ValueError, match="policy"):
        await task
    assert not await target.database.read("SELECT * FROM fed_inbox_item")
    assert not await target.database.read("SELECT * FROM fed_revision_receipt")


@pytest.mark.parametrize(
    "fault",
    [
        "unnegotiated",
        "bad_code",
        "wrong_uid",
        "bad_revision",
        "missing_target",
        "invalid_mode",
        "wrong_sender",
        "no_event",
    ],
)
async def test_malformed_compact_event_cannot_store_or_trigger_an_alert(nodes, fault):
    source, sp, target, tp, _, event = await receive_fixture(nodes)
    if fault != "unnegotiated":
        await capability(source, sp)
        tp = await capability(target, tp)
    value = {
        "mode": 2,
        "target_mesh_id": "!local",
        "event": {**copy.deepcopy(event), "payload": compact_payload(event["payload"])},
    }
    if fault == "bad_code":
        value["event"]["payload"][999] = "unknown code"
    elif fault == "wrong_uid":
        value["event"]["payload"][0] = "!someone:else"
    elif fault == "bad_revision":
        value["event"]["revision"] = True
    elif fault == "missing_target":
        del value["target_mesh_id"]
    elif fault == "invalid_mode":
        value["mode"] = True
    elif fault == "wrong_sender":
        value["mesh_id"] = "!someone"
    elif fault == "no_event":
        value["event"] = []
    if fault == "wrong_sender":
        # The general wire fixture always supplies the real sender identity.
        with pytest.raises(ValueError, match="targeted incident event"):
            await target.federation_sync.incident_events.receive_wire(tp, value, 100)
    else:
        await wire(source, target, MessageType.INCIDENT, value)
    assert not await target.database.read("SELECT * FROM fed_inbox_item")
    assert not await target.database.read("SELECT * FROM incident")
    assert not await target.database.read("SELECT * FROM alert")
    assert not await target.database.read("SELECT * FROM outbound_work")
