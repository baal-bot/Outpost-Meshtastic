from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.render.renderer import render_response
from outpost.router.models import ResponseKind
from outpost.transport.governor import OutboundItem
from outpost.transport.models import InboundMessage, TrafficClass
from tests.support.application import fresh_install

pytestmark = pytest.mark.production_wiring


def config(path) -> Config:
    return Config.model_validate(
        {
            "store": {"path": str(path)},
            "modules": {"watch": {"enabled": True}},
            "channels": {0: {"name": "public"}, 3: {"name": "watch"}},
        }
    )


def inbound(
    packet_id: int,
    text: str,
    sender: str,
    *,
    key: bytes | None = None,
    direct: bool = True,
) -> InboundMessage:
    return InboundMessage(
        packet_id=packet_id,
        from_id=sender,
        to_id="!outpost" if direct else "^all",
        channel=0,
        portnum=1,
        is_direct=direct,
        text=text if direct else f"!{text}",
        payload=None,
        rx_time=datetime.now(UTC),
        pki_encrypted=key is not None,
        pki_public_key=key,
    )


async def responder(app: OutpostApp, mesh_id: str, handle: str) -> bytes:
    key = bytes.fromhex(mesh_id.removeprefix("!").zfill(64))
    member = await app.router.members.resolve(mesh_id, authenticated_pki_key=key)
    await app.database.write(
        "UPDATE member SET handle=?,trust='responder',pki_state='verified',public_key=? WHERE id=?",
        (handle, key, member.id),
    )
    return key


@pytest.mark.asyncio
async def test_alert_and_ack_commands_enforce_trust_render_results_and_mutate_state(
    tmp_path,
) -> None:
    app = OutpostApp(config(tmp_path / "outpost.db"))
    await app.database.open()
    key = await responder(app, "!00000002", "ray")
    try:
        report = await app.router.dispatch(
            inbound(1, "REPORT fire at barn 40.4406 -79.9959", "!00000001")
        )
        assert render_response(report) == "✓ INC 1 fire · GPS 40.441,-79.996"

        guest_denied = await app.router.dispatch(
            inbound(2, "ALERT urgent 1 Barn fire", "!00000003")
        )
        assert guest_denied.kind == ResponseKind.ERROR
        assert render_response(guest_denied) == "Unknown. Send ? for help."

        pki_denied = await app.router.dispatch(inbound(3, "ALERT urgent 1 Barn fire", "!00000002"))
        assert "Elevated action denied" in render_response(pki_denied)

        malformed = await app.router.dispatch(inbound(4, "ALERT urgent", "!00000002", key=key))
        assert render_response(malformed).startswith("ALERT needs")
        bad_severity = await app.router.dispatch(
            inbound(5, "ALERT extreme 1 Barn fire", "!00000002", key=key)
        )
        assert render_response(bad_severity) == (
            "Alert severity must be caution, urgent, or critical."
        )
        raised = await app.router.dispatch(
            inbound(6, "ALERT urgent 1 Barn fire", "!00000002", key=key)
        )
        assert render_response(raised) == ("✓ ALERT 1 recorded for INC 1; 1 transmission queued.")
        assert len(await app.alerts.list()) == 1

        ack_usage = await app.router.dispatch(inbound(7, "ACK nope", "!00000004"))
        assert render_response(ack_usage) == "ACK needs incident number."
        missing = await app.router.dispatch(inbound(8, "ACK 99", "!00000005"))
        assert render_response(missing) == "No active alert for that incident."
        acked = await app.router.dispatch(inbound(9, "ACK 1 responding", "!00000006"))
        assert render_response(acked) == "✓ ack INC 1 · 1"
        assert (await app.alerts.list())[0].ack_count == 1
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_welfare_commands_cover_event_lifecycle_rosters_and_member_text(tmp_path) -> None:
    app = OutpostApp(config(tmp_path / "outpost.db"))
    await app.database.open()
    key = await responder(app, "!00000010", "lead")
    first = await app.router.members.resolve("!00000011")
    second = await app.router.members.resolve("!00000012")
    await app.router.members.claim_handle(first.mesh_id, "alex")
    await app.router.members.claim_handle(second.mesh_id, "sam")
    try:
        ok = await app.router.dispatch(inbound(10, "OK home safe", first.mesh_id))
        assert "✓ ok" in render_response(ok) and "0/0 in." in render_response(ok)
        help_without_event = await app.router.dispatch(
            inbound(11, "HELPME road blocked", second.mesh_id)
        )
        assert "1 responder notified" in render_response(help_without_event)
        no_roster = await app.router.dispatch(inbound(12, "ROSTER", "!00000013"))
        assert render_response(no_roster) == "No open watch event."
        no_names = await app.router.dispatch(inbound(13, "ROSTER?", "!00000010", key=key))
        assert render_response(no_names) == "No open watch event."

        trust_denied = await app.router.dispatch(inbound(14, "EVENT OPEN all Storm", "!00000014"))
        assert render_response(trust_denied) == "Unknown. Send ? for help."
        usage = await app.router.dispatch(inbound(15, "EVENT OPEN invalid", "!00000010", key=key))
        assert render_response(usage) == "EVENT OPEN <all|responders|subscribed> <name>."
        opened = await app.router.dispatch(
            inbound(16, "EVENT OPEN all Ice storm", "!00000010", key=key)
        )
        assert render_response(opened) == '✓ Event "Ice storm" opened.'
        duplicate = await app.router.dispatch(
            inbound(17, "EVENT OPEN all Flood", "!00000010", key=key)
        )
        assert render_response(duplicate) == "Close the current watch event first."

        checked = await app.router.dispatch(inbound(18, "OK warm", first.mesh_id))
        assert "1/3 in." in render_response(checked)
        needs_help = await app.router.dispatch(inbound(19, "HELPME water rising", second.mesh_id))
        assert "1 responder notified" in render_response(needs_help)
        roster = await app.router.dispatch(inbound(20, "ROSTER", "!00000015"))
        assert render_response(roster).startswith('Event "Ice storm": 1 ok · 1 help')
        names = await app.router.dispatch(inbound(21, "ROSTER?", "!00000010", key=key))
        assert "alex · ok" in render_response(names)
        assert "sam · need_help" in render_response(names)

        closed = await app.router.dispatch(inbound(22, "EVENT CLOSE", "!00000010", key=key))
        assert render_response(closed) == '✓ Event "Ice storm" closed.'
        no_event = await app.router.dispatch(inbound(23, "EVENT CLOSE", "!00000010", key=key))
        assert render_response(no_event) == "No open watch event."
        invalid = await app.router.dispatch(inbound(24, "EVENT PAUSE", "!00000010", key=key))
        assert render_response(invalid) == "EVENT OPEN <policy> <name>, or EVENT CLOSE."
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_incident_commands_render_validation_reactions_lists_and_detail(tmp_path) -> None:
    app = OutpostApp(config(tmp_path / "outpost.db"))
    await app.database.open()
    try:
        empty = await app.router.dispatch(inbound(30, "INCIDENTS", "!00000020"))
        assert render_response(empty) == "No active incidents."
        invalid_report = await app.router.dispatch(inbound(31, "REPORT", "!00000021"))
        assert render_response(invalid_report) == "REPORT needs details."
        filed = await app.router.dispatch(
            inbound(32, "REPORT tree blocking cedar road", "!00000022")
        )
        assert render_response(filed).startswith("✓ INC 1 hazard. No location")
        located = await app.router.dispatch(
            inbound(33, "REPORT fire at warehouse 40.4406 -79.9959", "!00000023")
        )
        assert render_response(located) == "✓ INC 2 fire · GPS 40.441,-79.996"
        similar = await app.router.dispatch(
            inbound(34, "REPORT fire at warehouse 40.4406 -79.9959", "!00000024")
        )
        assert render_response(similar).startswith("Similar: INC 2 fire")
        forced = await app.router.dispatch(
            inbound(35, "REPORT! tree blocking cedar road", "!00000025")
        )
        assert render_response(forced) == "✓ INC 3 hazard filed."

        bad_confirm = await app.router.dispatch(inbound(36, "CONFIRM nope", "!00000026"))
        assert render_response(bad_confirm) == "CONFIRM needs incident number."
        confirmed = await app.router.dispatch(inbound(37, "CONFIRM 1", "!00000027"))
        assert render_response(confirmed) == "✓ INC 1 confirmed · ✓1"
        bad_dispute = await app.router.dispatch(inbound(38, "DISPUTE nope", "!00000028"))
        assert render_response(bad_dispute) == "DISPUTE needs incident number [note]."
        disputed = await app.router.dispatch(inbound(39, "DISPUTE 1 road passable", "!00000029"))
        assert render_response(disputed) == "✓ INC 1 disputed · 1"

        listing = await app.router.dispatch(inbound(40, "INCIDENTS", "!00000030"))
        assert listing.screen is not None and listing.screen.title == "ACTIVE INCIDENTS"
        broadcast = await app.router.dispatch(inbound(41, "INCIDENTS", "!00000031", direct=False))
        assert "active · no position" in render_response(broadcast)
        bad_detail = await app.router.dispatch(inbound(42, "INC nope", "!00000032"))
        assert render_response(bad_detail) == "INC needs incident number."
        missing = await app.router.dispatch(inbound(43, "INC 99", "!00000033"))
        assert render_response(missing) == "No incident."
        detail = await app.router.dispatch(inbound(44, "INC 1", "!00000034"))
        assert detail.screen is not None and detail.screen.title == "INCIDENT 1"
        assert "confirm @!00000027: noted" in render_response(detail)
        assert "dispute @!00000029: road passable" in render_response(detail)
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_blocked_radio_cannot_invoke_any_safety_command(tmp_path) -> None:
    app = OutpostApp(config(tmp_path / "outpost.db"))
    await app.database.open()
    member = await app.router.members.resolve("!00000040")
    await app.database.write("UPDATE member SET trust='blocked' WHERE id=?", (member.id,))
    try:
        for packet_id, command in enumerate(
            (
                "ALERT urgent 1 Test",
                "ACK 1",
                "CONFIRM 1",
                "DISPUTE 1",
                "OK",
                "HELPME",
                "REPORT test",
            ),
            start=50,
        ):
            response = await app.router.dispatch(inbound(packet_id, command, member.mesh_id))
            assert response.kind == ResponseKind.NONE
        assert await app.database.read("SELECT id FROM incident") == []
        assert await app.database.read("SELECT id FROM checkin") == []
        assert await app.database.read("SELECT id FROM alert") == []
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_refused_command_reply_is_linked_to_inbound_conversation(tmp_path) -> None:
    value = config(tmp_path / "outpost.db")
    value = value.model_copy(
        update={"airtime": value.airtime.model_copy(update={"queue_max_items": 1})}
    )
    app = OutpostApp(value)
    await app.database.open()
    try:
        filler = await app.governor.admit(OutboundItem("busy", "!peer", 0, TrafficClass.DIGEST))
        assert filler is not None
        message = inbound(70, "PING", "!00000070")
        inbound_id = await app.message_log.record_inbound(message)

        assert await app._handle_inbound_safely(message, inbound_id)

        dropped = await app.database.read(
            "SELECT direction,outcome,drop_reason,in_reply_to_id,command FROM message_log "
            "WHERE direction='out'"
        )
        assert [dict(row) for row in dropped] == [
            {
                "direction": "out",
                "outcome": "dropped",
                "drop_reason": "queue_full",
                "in_reply_to_id": inbound_id,
                "command": "PING",
            }
        ]
    finally:
        await app.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("requester_is_responder", [False, True])
async def test_helpme_plainly_warns_when_no_other_responder_is_reached(
    tmp_path, requester_is_responder: bool
) -> None:
    app = OutpostApp(config(tmp_path / "outpost.db"))
    await app.database.open()
    try:
        member = await app.router.members.resolve("!00000080")
        if requester_is_responder:
            await app.database.write("UPDATE member SET trust='responder' WHERE id=?", (member.id,))
        response = await app.router.dispatch(inbound(80, "HELPME trapped", member.mesh_id))
        text = render_response(response)
        assert text.startswith("⚠ No responder was reached. Contact 911")
        assert "Responders notified" not in text
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_fresh_install_safety_paths_make_zero_delivery_explicit(tmp_path) -> None:
    value = config(tmp_path / "outpost.db")
    value = value.model_copy(
        update={
            "watch": value.watch.model_copy(
                update={"emergency_keywords_enabled": True, "emergency_keywords": ["mayday"]}
            )
        }
    )
    async with fresh_install(
        value,
        member_mesh_ids=("!00000091", "!00000092", "!00000093"),
    ) as app:
        alert = await app.alerts.raise_alert("urgent", "Bridge failure", "operator")
        assert alert.escalation_stage == 0
        assert alert.delivery_state == "empty_audience"
        assert alert.last_delivery_count == 0

        checked = await app.router.dispatch(inbound(91, "OK safe", "!00000091"))
        assert render_response(checked).startswith("✓ ok")
        checkin = (
            await app.database.read(
                "SELECT c.status FROM checkin c JOIN member m ON m.id=c.member_id "
                "WHERE m.mesh_id='!00000091'"
            )
        )[0]
        assert checkin["status"] == "ok"

        help_response = await app.router.dispatch(inbound(92, "HELPME trapped", "!00000092"))
        assert render_response(help_response).startswith("⚠ No responder was reached. Contact 911")

        await app._handle_inbound_message(inbound(93, "mayday injured on ridge", "!00000093"))
        incident = (
            await app.database.read("SELECT notification_state,notification_count FROM incident")
        )[0]
        assert dict(incident) == {
            "notification_state": "empty_audience",
            "notification_count": 0,
        }


@pytest.mark.asyncio
async def test_helpme_warns_when_queue_policy_refuses_responder_delivery(tmp_path) -> None:
    value = config(tmp_path / "outpost.db")
    value = value.model_copy(
        update={"airtime": value.airtime.model_copy(update={"queue_max_items": 1})}
    )
    app = OutpostApp(value)
    await app.database.open()
    try:
        await responder(app, "!00000081", "responder")
        filler = await app.governor.admit(OutboundItem("busy", "!peer", 0, TrafficClass.DIGEST))
        assert filler is not None
        response = await app.router.dispatch(inbound(81, "HELPME trapped", "!00000082"))
        assert render_response(response).startswith("⚠ No responder was reached. Contact 911")
        row = (await app.database.read("SELECT notification_state FROM checkin"))[0]
        assert row["notification_state"] == "refused"
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_emergency_keyword_zero_delivery_is_visible_on_incident(tmp_path) -> None:
    value = config(tmp_path / "outpost.db")
    value = value.model_copy(
        update={
            "watch": value.watch.model_copy(
                update={"emergency_keywords_enabled": True, "emergency_keywords": ["mayday"]}
            )
        }
    )
    app = OutpostApp(value)
    await app.database.open()
    try:
        await app._handle_inbound_message(inbound(90, "mayday injured on ridge", "!00000090"))
        incident = (
            await app.database.read("SELECT notification_state,notification_count FROM incident")
        )[0]
        assert dict(incident) == {
            "notification_state": "empty_audience",
            "notification_count": 0,
        }
        assert await app.database.read(
            "SELECT 1 FROM audit_log WHERE action='safety.delivery.zero' AND target='incident:1'"
        )
        assert (await app.database.read("SELECT state FROM mail"))[0]["state"] == "failed"
    finally:
        await app.database.close()
