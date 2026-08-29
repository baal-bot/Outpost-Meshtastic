from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.render import render_response
from outpost.router.models import Response
from outpost.router.session import TuiConfirmation
from outpost.transport.chunker import chunk_text
from outpost.transport.governor import OutboundItem
from outpost.transport.models import InboundMessage, TrafficClass
from outpost.web.member_triage import MemberTriageService


def packet(
    packet_id: int,
    sender: str,
    text: str,
    *,
    key: bytes | None,
    direct: bool = True,
) -> InboundMessage:
    return InboundMessage(
        packet_id=packet_id,
        from_id=sender,
        to_id="!699c2f30" if direct else "^all",
        channel=0,
        portnum=1,
        is_direct=direct,
        text=text if direct else f"!{text}",
        payload=None,
        rx_time=datetime.now(UTC),
        pki_encrypted=key is not None,
        pki_public_key=key,
    )


def config(path) -> Config:
    return Config.model_validate(
        {
            "store": {"path": str(path)},
            "modules": {
                "bbs": {"enabled": True},
                "watch": {"enabled": True},
                "fed": {"enabled": True},
            },
            "channels": {0: {"name": "public", "alerts": True, "bbs": "full"}},
        }
    )


async def grant(app: OutpostApp, mesh_id: str, key: bytes, trust: str):  # type: ignore[no-untyped-def]
    member = await app.router.members.resolve(mesh_id, authenticated_pki_key=key)
    triage = MemberTriageService(app.database)
    await triage.review_pki(member.id, "approve", "Fingerprint verified for OPS test")
    await triage.update(
        member.id,
        trust=trust,
        notes=None,
        notes_supplied=False,
        reason="Assigned incident response role",
    )
    return await app.router.members.resolve(mesh_id)


async def dispatch(
    app: OutpostApp,
    packet_id: int,
    sender: str,
    text: str,
    key: bytes,
    *,
    direct: bool = True,
) -> tuple[str, Response]:
    response = await app.router.dispatch(packet(packet_id, sender, text, key=key, direct=direct))
    return render_response(response), response


@pytest.mark.asyncio
async def test_ops_responder_transcript_is_private_bounded_and_snapshot_stable(tmp_path) -> None:
    app = OutpostApp(config(tmp_path / "outpost.db"))
    await app.database.open()
    responder_id = "!00000001"
    key = bytes(range(32))
    try:
        responder = await grant(app, responder_id, key, "responder")
        for number in range(1, 6):
            await app.incidents.create(
                f"road Original incident {number} at 40.{number},-80.{number}",
                None,
                force=True,
            )
        event = await app.checkins.open_event("Flood accountability", "all", "web:operator")
        other = await app.router.members.resolve("!00000002")
        await app.database.write("UPDATE member SET trust='member' WHERE id=?", (other.id,))
        await app.database.write(
            "INSERT INTO checkin(member_id,event_id,status,note,lat,lon,created_at) "
            "VALUES(?,?,'need_help','private medical note',40.123,-80.456,?)",
            (other.id, event.id, int(app.clock.now().timestamp())),
        )
        failed_id = await app.governor.admit(
            OutboundItem("private failed payload", "!00000009", 0, TrafficClass.REPLY)
        )
        await app.database.write(
            "UPDATE outbound_work SET state='failed',attempts=3,last_attempt_at=?,"
            "completed_at=?,last_error='ConnectionError: /private/device/path' WHERE id=?",
            (int(app.clock.now().timestamp()), int(app.clock.now().timestamp()), failed_id),
        )

        no_key = render_response(
            await app.router.dispatch(packet(1, responder_id, "OPS", key=None))
        )
        assert "verified PKI" in no_key
        ambiguous, _ = await dispatch(app, 58, responder_id, "OPZ", key)
        assert "Nothing was run" in ambiguous or "Command not run" in ambiguous
        broadcast, _ = await dispatch(app, 2, responder_id, "OPS", key, direct=False)
        assert "Elevated action denied" in broadcast
        assert "OUTPOST / OPS" not in broadcast

        home, response = await dispatch(app, 3, responder_id, "OPS", key)
        assert home.startswith("OUTPOST / OPS")
        assert "Action needed" in home and "Delivery failures" in home
        assert "Inbox" not in home and "Federation" not in home
        assert "private" not in home
        assert response.max_parts == 3
        assert chunk_text(home) == [home]

        welfare, welfare_response = await dispatch(app, 50, responder_id, "OPS WELFARE", key)
        assert "private medical note" not in welfare
        assert "40.123" not in welfare and "-80.456" not in welfare
        assert len(chunk_text(welfare, max_parts=welfare_response.max_parts or 1)) <= 3
        person, _ = await dispatch(app, 51, responder_id, "1", key)
        assert "NEED HELP" in person
        assert "notes and coordinates withheld" in person
        assert "private medical note" not in person

        first, _ = await dispatch(app, 4, responder_id, "OPS INCS", key)
        assert "metadata only; no coordinates" in first
        assert "40." not in first and "-80." not in first
        coordinate_detail, _ = await dispatch(app, 56, responder_id, "1", key)
        assert "[location withheld]" in coordinate_detail
        assert "40." not in coordinate_detail and "-80." not in coordinate_detail
        first, _ = await dispatch(app, 57, responder_id, "OPS INCS", key)
        snapshot = list(app.router.sessions.get(responder.mesh_id, -1).tui_snapshots["incidents"])
        new_incident, _ = await app.incidents.create(
            "fire New incident must not shift the open page", None, force=True
        )
        assert new_incident is not None and str(new_incident.id) not in snapshot

        second, _ = await dispatch(app, 5, responder_id, "4", key)
        assert "Page 2/2" in second
        assert "New incident" not in second
        assert "INC 2" in second and "INC 1" in second
        where, _ = await dispatch(app, 6, responder_id, "WHERE", key)
        assert "ops incidents page 2" in where.lower()
        back, _ = await dispatch(app, 7, responder_id, "BACK", key)
        assert "Back:" in back

        denied, _ = await dispatch(app, 8, responder_id, "OPS INBOX", key)
        assert "Operator role required; nothing changed." in denied
        failure_list, _ = await dispatch(app, 9, responder_id, "OPS FAIL", key)
        assert "payloads and internal errors withheld" in failure_list
        safe_failure = await app.operations_center.failure(failed_id)
        assert safe_failure is not None
        assert "text" not in safe_failure and "last_error" not in safe_failure
        failure, _ = await dispatch(app, 10, responder_id, "1", key)
        assert "retry exhaustion" in failure
        assert "private failed payload" not in failure
        assert "/private/device/path" not in failure

        global_home, _ = await dispatch(app, 52, responder_id, "HOME", key)
        assert global_home.startswith("OUTPOST / HOME")
        assert not app.router.sessions.get(responder.mesh_id, -1).tui_snapshots
        await dispatch(app, 53, responder_id, "OPS", key)
        global_help, _ = await dispatch(app, 54, responder_id, "?", key)
        assert global_help.startswith("OUTPOST / HOME")

        await app._handle_inbound_message(packet(55, responder_id, "OPS", key=key), ordered=True)
        replies = await app.database.read(
            "SELECT text,traffic_class,multipart FROM outbound_work "
            "WHERE destination=? AND id<>? ORDER BY id",
            (responder_id, failed_id),
        )
        assert len(replies) == 1
        assert replies[0]["traffic_class"] == "reply"
        assert len(str(replies[0]["text"]).encode()) <= 200
        assert replies[0]["multipart"] == 0
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_web_federation_import_uses_the_matching_audit_action(tmp_path, monkeypatch) -> None:
    app = OutpostApp(config(tmp_path / "outpost.db"))
    await app.database.open()
    calls: list[tuple[int, str]] = []

    async def imported(item_id: int, actor: str) -> str:
        calls.append((item_id, actor))
        return "incidents"

    monkeypatch.setattr(app, "import_federation_inbox_as", imported)
    try:
        assert await app.import_federation_inbox(42) == "incidents"
        assert calls == [(42, "web:operator")]
        audits = await app.database.read(
            "SELECT actor_kind,actor_ref,action,target,outcome FROM audit_log "
            "WHERE action='federation.inbox.import'"
        )
        assert [dict(row) for row in audits] == [
            {
                "actor_kind": "web",
                "actor_ref": "operator",
                "action": "federation.inbox.import",
                "target": "federation-inbox:42",
                "outcome": "success",
            }
        ]
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_ops_operator_mutations_are_confirmed_audited_and_one_shot(tmp_path) -> None:
    app = OutpostApp(config(tmp_path / "outpost.db"))
    await app.database.open()
    operator_id = "!00000666"
    key = bytes(reversed(range(32)))
    imports: list[tuple[int, str]] = []

    async def importer(item_id: int, actor: str) -> str:
        imports.append((item_id, actor))
        await app.database.write(
            "UPDATE fed_inbox_item SET state='imported',reviewed_at=?,reviewed_by=? WHERE id=?",
            (int(app.clock.now().timestamp()), actor, item_id),
        )
        return "incidents"

    app.operations_center.importer = importer
    try:
        operator = await grant(app, operator_id, key, "operator")
        incident, _ = await app.incidents.create(
            "road Bridge inspection required", None, force=True
        )
        assert incident is not None
        event = await app.checkins.open_event("Storm roster", "all", "web:operator")
        peer = await app.federation.discover("!bbbbbbbb", "Remote Outpost", 1, {}, "radio")
        app.radio._local_id = "!aaaaaaaa"
        app.federation.local_mesh_id = "!aaaaaaaa"
        await app.database.write(
            "UPDATE fed_peer SET state='active',shared_secret=?,relay_mail=1,"
            "local_approved=1,remote_approved=1 WHERE id=?",
            (bytes(range(32)), peer.id),
        )
        expires = int(app.clock.now().timestamp()) + 86_400
        await app.database.write(
            "INSERT INTO mail(uid,from_label,to_label,subject,body,created_at,state,expires_at,"
            "conversation_key,participant_handle,message_kind,mail_direction) "
            "VALUES('local-ops','field','operator','Local review','local secret body',200,"
            "'delivered',?,'local:ops','field','member','in')",
            (expires,),
        )
        await app.database.write(
            "INSERT INTO mail(uid,from_label,to_label,subject,body,created_at,state,expires_at,"
            "conversation_key,federation_conversation_id,participant_handle,message_kind,"
            "mail_direction,source_peer_mesh_id,reply_recipient_handle) "
            "VALUES('fed-ops','remote','operator','Remote review','federated secret body',100,"
            "'delivered',?,'fed:ops','conversation-1','remote','member','in',?, 'remote')",
            (expires, peer.mesh_id),
        )
        inbox_id = await app.database.write(
            "INSERT INTO fed_inbox_item(peer_id,stream,uid,payload_json,digest,received_at) "
            "VALUES(?,'incidents','remote:inc:1','{\"body\":\"private imported body\"}',"
            "'digest-1',300)",
            (peer.id,),
        )

        home, _ = await dispatch(app, 19, operator_id, "OPS", key)
        assert "Inbox" in home and "Federation" not in home
        assert chunk_text(home) == [home]

        inbox, _ = await dispatch(app, 20, operator_id, "OPS INBOX", key)
        assert "message bodies withheld" in inbox
        local_detail, _ = await dispatch(app, 21, operator_id, "1", key)
        assert "Local review" in local_detail and "local secret body" not in local_detail
        archive_prompt, _ = await dispatch(app, 22, operator_id, "1", key)
        assert "Nothing changed yet" in archive_prompt
        assert (
            await app.database.read(
                "SELECT archived_at FROM mail WHERE conversation_key='local:ops'"
            )
        )[0]["archived_at"] is None
        session = app.router.sessions.get(operator.mesh_id, -1)
        archive_token = next(iter(session.tui_confirmations))
        archived, _ = await dispatch(app, 23, operator_id, "1", key)
        assert "Conversation archived" in archived
        assert (
            await app.database.read(
                "SELECT archived_at FROM mail WHERE conversation_key='local:ops'"
            )
        )[0]["archived_at"] is not None
        replay, _ = await dispatch(app, 24, operator_id, f"OPS DO {archive_token}", key)
        assert "already used" in replay

        await dispatch(app, 25, operator_id, "OPS INBOX", key)
        remote_detail, _ = await dispatch(app, 26, operator_id, "1", key)
        assert "Remote review" in remote_detail and "federated secret body" not in remote_detail
        reply_input, _ = await dispatch(app, 27, operator_id, "2", key)
        assert "nothing sent yet" in reply_input
        reply_confirm, _ = await dispatch(app, 28, operator_id, "Team will review at 18:00.", key)
        assert "SEND OPERATIONS REPLY?" in reply_confirm
        assert "Team will review" not in reply_confirm
        reply_token = next(iter(session.tui_confirmations))
        replied, _ = await dispatch(app, 29, operator_id, "1", key)
        assert "governed federation delivery" in replied
        sent_mail = await app.database.read(
            "SELECT body,operator_actor FROM mail WHERE mail_direction='out'"
        )
        assert [dict(row) for row in sent_mail] == [
            {"body": "Team will review at 18:00.", "operator_actor": f"mesh:{operator_id}"}
        ]
        queued_reply = await app.database.read(
            "SELECT traffic_class,want_ack FROM outbound_work WHERE binary_payload IS NOT NULL"
        )
        assert queued_reply and {row["traffic_class"] for row in queued_reply} == {"reply"}
        assert all(row["want_ack"] == 0 for row in queued_reply)
        replay_reply, _ = await dispatch(app, 30, operator_id, f"OPS DO {reply_token}", key)
        assert "already used" in replay_reply
        assert len(await app.database.read("SELECT id FROM mail WHERE mail_direction='out'")) == 1

        await dispatch(app, 31, operator_id, "OPS INCS", key)
        await dispatch(app, 32, operator_id, "1", key)
        resolve_note, _ = await dispatch(app, 33, operator_id, "2", key)
        assert "resolution note" in resolve_note
        resolve_confirm, _ = await dispatch(
            app, 34, operator_id, "Inspection complete; bridge open.", key
        )
        assert "Nothing changed yet" in resolve_confirm
        assert (await app.incidents.by_id(incident.id)).status == "open"  # type: ignore[union-attr]
        resolved, _ = await dispatch(app, 35, operator_id, "1", key)
        assert "resolved; audit recorded" in resolved
        assert (await app.incidents.by_id(incident.id)).status == "resolved"  # type: ignore[union-attr]

        close_prompt, _ = await dispatch(app, 36, operator_id, f"OPS CLOSE {event.id}", key)
        assert "CLOSE WELFARE EVENT?" in close_prompt
        closed, _ = await dispatch(app, 37, operator_id, "1", key)
        assert "closed; audit recorded" in closed
        assert (await app.checkins.by_id(event.id)).closed_at is not None  # type: ignore[union-attr]

        await dispatch(app, 38, operator_id, "OPS REVIEWS", key)
        review, _ = await dispatch(app, 39, operator_id, "1", key)
        assert "content withheld until import" in review
        import_prompt, _ = await dispatch(app, 40, operator_id, "1", key)
        assert "Nothing changed yet" in import_prompt
        imported, _ = await dispatch(app, 41, operator_id, "1", key)
        assert "imported; audit recorded" in imported
        assert imports == [(inbox_id, f"mesh:{operator_id}")]

        await app.database.write(
            "INSERT INTO mail(uid,from_label,to_label,subject,body,created_at,state,expires_at,"
            "conversation_key,participant_handle,message_kind,mail_direction) "
            "VALUES('timeout-ops','field2','operator','Timeout review','must remain',400,"
            "'delivered',?,'local:timeout','field2','member','in')",
            (expires,),
        )
        await dispatch(app, 42, operator_id, "OPS INBOX", key)
        await dispatch(app, 43, operator_id, "1", key)
        timeout_prompt, _ = await dispatch(app, 44, operator_id, "1", key)
        assert "ARCHIVE CONVERSATION?" in timeout_prompt
        timeout_token = next(iter(session.tui_confirmations))
        confirmation = session.tui_confirmations[timeout_token]
        session.tui_confirmations[timeout_token] = TuiConfirmation(
            confirmation.action, confirmation.target, confirmation.payload, -1
        )
        assert session.pending is not None
        session.pending.expires_at = -1
        timed_out, _ = await dispatch(app, 45, operator_id, "1", key)
        assert timed_out == "No active menu. Send ? to start again."
        assert (
            await app.database.read(
                "SELECT archived_at FROM mail WHERE conversation_key='local:timeout'"
            )
        )[0]["archived_at"] is None

        encoded_timeout = base64.urlsafe_b64encode(b"local:timeout").decode().rstrip("=")
        interruption_prompt, _ = await dispatch(
            app, 59, operator_id, f"OPS ARCHIVE {encoded_timeout}", key
        )
        assert "ARCHIVE CONVERSATION?" in interruption_prompt
        interrupted_token = next(iter(session.tui_confirmations))
        ping, _ = await dispatch(app, 60, operator_id, "PING", key)
        assert "pong" in ping
        interrupted_action, _ = await dispatch(
            app, 61, operator_id, f"OPS DO {interrupted_token}", key
        )
        assert "interrupted" in interrupted_action
        assert (
            await app.database.read(
                "SELECT archived_at FROM mail WHERE conversation_key='local:timeout'"
            )
        )[0]["archived_at"] is None

        reconnect_prompt, _ = await dispatch(
            app, 62, operator_id, f"OPS ARCHIVE {encoded_timeout}", key
        )
        assert "ARCHIVE CONVERSATION?" in reconnect_prompt
        reconnect_token = next(iter(session.tui_confirmations))
        await app.reconnect_radio()
        after_reconnect, _ = await dispatch(app, 63, operator_id, f"OPS DO {reconnect_token}", key)
        assert "interrupted" in after_reconnect
        assert (
            await app.database.read(
                "SELECT archived_at FROM mail WHERE conversation_key='local:timeout'"
            )
        )[0]["archived_at"] is None

        encoded = base64.urlsafe_b64encode(b"fed:ops").decode().rstrip("=")
        interrupted, _ = await dispatch(app, 46, operator_id, f"OPS REPLY {encoded}", key)
        assert "Send reply text" in interrupted
        second_ping, _ = await dispatch(app, 47, operator_id, "PING", key)
        assert "pong" in second_ping
        abandoned, _ = await dispatch(app, 48, operator_id, "This must not send.", key)
        assert "Not an option" in abandoned or "Unknown" in abandoned
        assert len(await app.database.read("SELECT id FROM mail WHERE mail_direction='out'")) == 1

        audits = await app.database.read(
            "SELECT actor_kind,actor_ref,action,outcome FROM audit_log "
            "WHERE actor_kind='mesh' AND actor_ref=? AND action NOT LIKE 'mesh.elevated_auth'",
            (operator_id,),
        )
        assert {row["action"] for row in audits} >= {
            "incident.update",
            "event.close",
            "mail.conversation.archive",
            "mail.conversation.reply",
            "federation.inbox.import",
        }
        assert all(row["outcome"] == "success" for row in audits)
    finally:
        await app.database.close()
