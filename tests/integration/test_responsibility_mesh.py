"""Actual private intake/router/governor and partition-delayed federation boundaries."""

import json
from dataclasses import replace

import pytest

from outpost.fed.framing import MessageType
from outpost.render import render_response
from outpost.router.models import DispatchTrace
from outpost.store.members import MemberRepo
from outpost.transport.chunker import chunk_text
from outpost.watch.responsibility import ResponsibilityActor
from tests.integration.test_federation_incident_events import envelope, prepare
from tests.integration.test_federation_incident_notes import approve, exported, preview
from tests.integration.test_federation_item_failures import wire
from tests.integration.test_federation_revisions import nodes as nodes
from tests.integration.test_incident_responsibility import KEY
from tests.integration.test_incident_responsibility import coordination as coordination
from tests.integration.test_mesh_operations_center import packet
from tests.integration.test_outage_readiness import appliance as appliance

pytestmark = pytest.mark.production_wiring


async def test_handheld_private_task_transcript_crosses_real_intake_and_governed_replies(
    coordination,
):
    app, incident, actors, _ = coordination
    await app.radio.connect()
    target = (await app.incidents.responsibility.targets("group"))[0]["reference"]
    sequence = 1000

    async def dispatch(sender, text, *, key=KEY, direct=True):
        nonlocal sequence
        sequence += 1
        app.clock.advance(61)
        message = replace(
            packet(sequence, sender, text, key=key, direct=direct),
            rx_time=app.clock.now(),
            to_id=app.radio.local_node_id if direct else "^all",
        )
        trace = DispatchTrace()
        before = len(await app.database.read("SELECT id FROM outbound_work"))
        await app._handle_inbound_message(message, trace=trace)
        after = await app.database.read("SELECT text,destination FROM outbound_work ORDER BY id")
        created = after[before:]
        assert all(row["destination"] == sender for row in created)
        assert len(created) <= 3
        assert all("…MORE" not in row["text"] for row in created)
        return trace

    now = int(app.clock.now().timestamp())
    await app.database.write(
        "INSERT INTO pending_incident_location(member_id,lat,lon,created_at,expires_at) "
        "VALUES(?,40.44061,-79.99591,?,?)",
        (actors[0].member_id, now, now + 3600),
    )
    home = await dispatch("!00000002", f"TASK {incident.local_ref}")
    assert home.resolved_command == "TASK" and "Token " in home.response_text
    token = (await app.incidents.responsibility.snapshot(incident.id))["review_token"]
    offer = await dispatch(
        "!00000002",
        f"TASK {incident.local_ref} offer {token} group:{target} help private coordination",
    )
    assert (
        offer.resolved_command == "TASK"
        and "Pending acceptance: Search team" in offer.response_text
    )
    assert len(await app.database.read("SELECT id FROM incident")) == 1
    assert await app.database.read(
        "SELECT 1 FROM pending_incident_location WHERE member_id=?", (actors[0].member_id,)
    )
    assert not await app.database.read("SELECT id FROM alert")
    denied = await dispatch("!00000003", f"TASK {incident.local_ref}", key=None)
    assert denied.response_kind == "error" and "Search team" not in denied.response_text
    broadcast = await dispatch("!00000003", f"TASK {incident.local_ref} help private", direct=False)
    assert broadcast.response_kind == "error" and "Search team" not in broadcast.response_text
    await dispatch("!00000003", f"TASK {incident.local_ref}")
    session = app.router.sessions.get("!00000003", -1)
    acceptance = next(
        choice
        for choice, command in session.pending.choices.items()
        if choice.isdigit() and " accept " in command
    )
    now = int(app.clock.now().timestamp())
    await app.database.write(
        "INSERT INTO pending_incident_location(member_id,lat,lon,created_at,expires_at) "
        "VALUES(?,40.44061,-79.99591,?,?)",
        (actors[1].member_id, now, now + 3600),
    )
    accepted = await dispatch("!00000003", acceptance)
    assert accepted.resolved_command == "TASK" and "Owner: Search team" in accepted.response_text
    assert len(await app.database.read("SELECT id FROM incident")) == 1
    # Let the unchanged governor send a synthetic local reply; no direct radio shortcut.
    for _ in range(5):
        await app.governor.tick()
        app.clock.advance(15)
    assert app.radio.sent and all(p.dest.startswith("!") for p in app.radio.sent)


async def test_handheld_syntax_pages_and_state_transitions_are_bounded(coordination):
    app, incident, actors, _ = coordination
    sequence = 2000

    async def command(text, sender="!00000002"):
        nonlocal sequence
        sequence += 1
        app.clock.advance(61)
        response = await app.router.dispatch(
            replace(packet(sequence, sender, text, key=KEY), rx_time=app.clock.now())
        )
        rendered = render_response(response)
        assert len(chunk_text(rendered, max_parts=3)) <= 3
        return response, rendered

    for args in (
        "",
        "-1",
        "9" * 20,
        "9999",
        f"{incident.local_ref} TARGETS",
        f"{incident.local_ref} TARGETS absent",
        f"{incident.local_ref} TARGETS group bad",
        f"{incident.local_ref} offer",
        f"{incident.local_ref} offer broken absent x",
        f"{incident.local_ref} cancel broken extra",
        f"{incident.local_ref} " + "x" * 220,
    ):
        response, _ = await command("TASK " + args)
        assert response.kind == "error"
    response, listing = await command(f"TASK {incident.local_ref} TARGETS member")
    assert response.kind == "listing" and "member:" in listing and "More: TASK" in listing
    response, listing = await command(f"TASK {incident.local_ref} TARGETS member 9999")
    assert response.kind == "listing" and "No more" in listing
    _, next_action = await command(f"TASK {incident.local_ref} NEXT")
    assert "No accepted next action" in next_action
    service = app.incidents.responsibility
    target = (await service.targets("member", query="responder3"))[0]["reference"]

    async def change(action, extra="", sender="!00000002"):
        token = (await service.snapshot(incident.id))["review_token"]
        response, rendered = await command(
            f"TASK {incident.local_ref} {action} {token} {extra}".strip(), sender
        )
        assert response.kind == "detail", rendered

    await change("offer", f"member:{target} Inspect road")
    await change("accept", sender="!00000003")
    await change("update", "Work underway", sender="!00000003")
    _, next_action = await command(f"TASK {incident.local_ref} NEXT", "!00000003")
    assert "Work underway" in next_action
    await change("release", sender="!00000003")
    assert (await service.snapshot(incident.id))["state"] == "released"


async def test_delayed_reviewed_federation_facts_cannot_export_or_overwrite_local_ownership(nodes):
    source, sp, target, _, incident, event = await prepare(nodes)
    await wire(source, target, MessageType.INCIDENT, envelope(event))
    await approve(target, await preview(target, event["uid"]))
    imported = (await target.database.read("SELECT id FROM incident WHERE uid=?", (event["uid"],)))[
        0
    ]["id"]

    async def local_owner(app, incident_id):
        member = await MemberRepo(app.database, app.clock).resolve("!00000003")
        await app.database.write(
            "UPDATE member SET trust='operator',pki_state='verified',public_key=?,"
            "handle='local-owner' WHERE id=?",
            (KEY, member.id),
        )
        actor = ResponsibilityActor(member_id=member.id, public_key=KEY)
        service = app.incidents.responsibility
        destination = (await service.targets("member"))[0]["reference"]
        token = (await service.snapshot(incident_id))["review_token"]
        offered = await service.apply(
            incident_id,
            actor,
            "offer",
            token,
            target_kind="member",
            target_ref=destination,
            next_action="Private assignment details never federated",
        )
        return await service.apply(incident_id, actor, "accept", offered["review_token"])

    await local_owner(source, incident.id)
    before = await local_owner(target, imported)
    assert "Private assignment" not in json.dumps(
        await exported(source, sp, "incidents", incident.uid)
    )
    source.clock.advance(60)
    await source.incidents.operator_patch(
        incident.id, status=None, severity="urgent", resolution=None, actor="web:remote"
    )
    update = await exported(source, sp, "incidents", incident.uid)
    event2 = {key: update[key] for key in ("stream", "uid", "epoch", "revision", "payload")}
    await wire(source, target, MessageType.INCIDENT, envelope(event2))
    await approve(target, await preview(target, event2["uid"]))
    after = await target.incidents.responsibility.snapshot(imported)
    assert after == before
    assert len(await target.incidents.responsibility.history(imported)) == 2
    assert not await target.database.read("SELECT id FROM alert")
