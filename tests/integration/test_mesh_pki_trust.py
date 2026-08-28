from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.router.models import ResponseKind
from outpost.transport.models import InboundMessage
from outpost.web.member_triage import MemberTriageError, MemberTriageService


def command(
    packet_id: int,
    text: str,
    *,
    key: bytes | None = None,
    direct: bool = True,
) -> InboundMessage:
    return InboundMessage(
        packet_id=packet_id,
        from_id="!00000001",
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


@pytest.mark.asyncio
async def test_elevated_mesh_commands_require_reviewed_direct_pki_and_block_replay(
    tmp_path,
) -> None:
    app = OutpostApp(
        Config.model_validate(
            {
                "store": {"path": str(tmp_path / "outpost.db")},
                "modules": {"watch": {"enabled": True}},
            }
        )
    )
    await app.database.open()
    triage = MemberTriageService(app.database)
    key = bytes(range(32))
    member = await app.router.members.resolve("!00000001", authenticated_pki_key=key)
    assert member.pki_state == "pending"

    with pytest.raises(MemberTriageError, match="approve"):
        await triage.update(
            member.id,
            trust="responder",
            notes=None,
            notes_supplied=False,
            reason="Known response lead",
        )
    reviewed = await triage.review_pki(member.id, "approve", "Fingerprint compared in person")
    assert reviewed["state"] == "verified"
    await triage.update(
        member.id,
        trust="responder",
        notes=None,
        notes_supplied=False,
        reason="Known response lead",
    )

    downgraded = await app.router.dispatch(command(10, "ROSTER?"))
    assert downgraded.kind == ResponseKind.ERROR
    assert "PKI" in downgraded.lines[0].text

    broadcast = await app.router.dispatch(command(11, "ROSTER?", key=key, direct=False))
    assert broadcast.kind == ResponseKind.ERROR
    assert "PKI" in broadcast.lines[0].text

    valid = await app.router.dispatch(command(12, "ROSTER?", key=key))
    assert valid.kind == ResponseKind.DETAIL
    replay = await app.router.dispatch(command(12, "ROSTER?", key=key))
    assert replay.kind == ResponseKind.ERROR
    assert "denied" in replay.lines[0].text

    reasons = await app.database.read(
        "SELECT json_extract(detail,'$.reason') reason FROM audit_log "
        "WHERE action='mesh.elevated_auth' ORDER BY id"
    )
    assert {row["reason"] for row in reasons} == {
        "direct_message_required",
        "pki_required",
        "replay",
    }
    assert (await app.database.read("SELECT COUNT(*) count FROM member_pki_replay"))[0][
        "count"
    ] == 1
    await app.database.close()

    restarted = OutpostApp(app.config)
    await restarted.database.open()
    try:
        durable_replay = await restarted.router.dispatch(command(12, "ROSTER?", key=key))
        assert durable_replay.kind == ResponseKind.ERROR
    finally:
        await restarted.database.close()


@pytest.mark.asyncio
async def test_pki_key_change_demotes_and_requires_explicit_rotation_review(tmp_path) -> None:
    app = OutpostApp(
        Config.model_validate(
            {
                "store": {"path": str(tmp_path / "outpost.db")},
                "modules": {"watch": {"enabled": True}},
            }
        )
    )
    await app.database.open()
    triage = MemberTriageService(app.database)
    old_key = bytes(range(32))
    new_key = bytes(reversed(range(32)))
    member = await app.router.members.resolve("!00000001", authenticated_pki_key=old_key)
    await triage.review_pki(member.id, "approve", "Original fingerprint verified")
    await triage.update(
        member.id,
        trust="operator",
        notes=None,
        notes_supplied=False,
        reason="Outpost operator radio",
    )
    assert (await app.router.dispatch(command(20, "OP STATUS", key=old_key))).kind == (
        ResponseKind.DETAIL
    )

    changed = await app.router.dispatch(command(21, "OP STATUS", key=new_key))
    assert changed.kind == ResponseKind.ERROR
    row = (
        await app.database.read(
            "SELECT trust,pki_state,public_key,pending_public_key FROM member WHERE id=?",
            (member.id,),
        )
    )[0]
    assert row["trust"] == "guest" and row["pki_state"] == "conflict"
    assert bytes(row["public_key"]) == old_key
    assert bytes(row["pending_public_key"]) == new_key
    assert await app.database.read(
        "SELECT 1 FROM audit_log WHERE action='member.pki.conflict' AND outcome='denied'"
    )
    with pytest.raises(ValueError, match="operator review"):
        await app.router.members.claim_handle("!00000001", "operator")
    review_queue = await triage.list(view="all", saved="review", query="", cursor=0, limit=20)
    assert review_queue["review_count"] == 1
    assert review_queue["items"][0]["pki_state"] == "conflict"

    await triage.review_pki(member.id, "approve", "Replacement radio verified in person")
    await triage.update(
        member.id,
        trust="operator",
        notes=None,
        notes_supplied=False,
        reason="Restored after verified key rotation",
    )
    rotated = await app.router.dispatch(command(22, "OP STATUS", key=new_key))
    assert rotated.kind == ResponseKind.DETAIL
    old_key_attempt = await app.router.dispatch(command(23, "OP STATUS", key=old_key))
    assert old_key_attempt.kind == ResponseKind.ERROR
    final = (
        await app.database.read("SELECT trust,pki_state FROM member WHERE id=?", (member.id,))
    )[0]
    assert final["trust"] == "guest" and final["pki_state"] == "conflict"
    await app.database.close()
