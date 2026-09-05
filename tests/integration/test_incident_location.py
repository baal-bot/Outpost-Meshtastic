from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.fed import FederationSyncService
from outpost.render import render_response
from outpost.router.models import ResponseKind
from outpost.store import Database
from outpost.transport.chunker import chunk_text
from outpost.transport.simulated import SimulatedRadioLink
from outpost.watch.incidents import IncidentService
from tests.integration.test_incident_reconciliation import active_incident_peer
from tests.integration.test_incident_transactions import interrupt_after_write
from tests.integration.test_safety_commands import inbound

pytestmark = [pytest.mark.asyncio, pytest.mark.production_wiring]
REPORTER = "!00000001"
KEY = b"\x01" * 32


@pytest.fixture
async def location_app(tmp_path) -> AsyncIterator[OutpostApp]:
    clock = VirtualClock()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "locations.db")},
            "modules": {"watch": {"enabled": True}, "env": {"enabled": True}},
            "env": {"user_agent": "Outpost isolated location tests (test@example.org)"},
        }
    )
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock))
    await app.database.open()
    await verified_member(app, REPORTER, KEY)
    try:
        yield app
    finally:
        await app.database.close()


async def verified_member(app: OutpostApp, sender: str, key: bytes):
    member = await app.router.members.resolve(sender, authenticated_pki_key=key)
    await app.database.write(
        "UPDATE member SET pki_state='verified',public_key=?,pending_public_key=NULL WHERE id=?",
        (key, member.id),
    )
    return await app.router.members.resolve(sender)


async def dispatch(app: OutpostApp, packet: int, text: str):
    return await app.router.dispatch(inbound(packet, text, REPORTER, key=KEY))


async def test_handheld_follows_exact_ack_then_corrects_without_cached_gps(location_app) -> None:
    app = location_app
    report = render_response(await dispatch(app, 1, "REPORT fallen tree by road"))
    command = re.search(r"UPD \d+ <where>", report)
    assert command and "public place; verified DM or ask operator" in report
    member = await app.router.members.resolve(REPORTER)
    await app.incidents.record_position(member, 40, -79, prompt=False)
    result = await dispatch(app, 2, command[0].replace("<where>", "North gate"))
    assert result.kind == ResponseKind.ACK
    assert "INC 1 location: North gate" in render_response(result)
    assert chunk_text(render_response(result)) == [render_response(result)]
    incident = await app.incidents.by_ref(1)
    assert incident and incident.location_text == "North gate"
    assert incident.lat is None and incident.lon is None and incident.location_unconfirmed
    corrected = await dispatch(app, 3, "UPD 1 South gate")
    assert corrected.kind == ResponseKind.ACK
    provenance = (await app.incidents.provenance(incident.id))[-1]
    assert provenance["event_kind"] == "location_corrected"
    assert provenance["actor"] == f"mesh:{REPORTER}"
    assert provenance["payload"]["before"]["location_text"] == "North gate"
    assert provenance["payload"]["after"]["location_text"] == "South gate"
    assert (await app.incidents.by_ref(1)).uid == incident.uid
    assert [row["seq"] for row in await app.incidents.updates(incident.id)] == [2, 1]
    for response in (report, render_response(corrected)):
        for token in re.findall(r"\b(?:send |Send |HELP |MENU )([A-Z][A-Z!?]+)\b", response):
            assert app.router.registry.resolve(token) is not None


async def test_guided_owner_location_choice_and_bare_text_keep_authentication(location_app) -> None:
    app = location_app
    await dispatch(app, 1, "MENU REPORT")
    filed = await dispatch(app, 2, "tree near road")
    assert "INC 1" in render_response(filed)
    detail = await dispatch(app, 3, "INC 1")
    assert detail.screen is not None
    choice = next(
        choice for choice in detail.screen.choices if choice.label == "Correct my location"
    )
    assert app.router.registry.resolve(choice.command.split()[0]) is not None
    menu = await dispatch(app, 4, choice.command)
    assert "verified DM" in render_response(menu)
    assert "-share" in render_response(menu)
    result = await dispatch(app, 5, "East entrance")
    assert "location: East entrance" in render_response(result)
    assert (await app.incidents.by_ref(1)).location_text == "East entrance"


@pytest.mark.parametrize(
    "attack", ["other_member", "plaintext", "broadcast", "wrong_key", "unreviewed", "replay"]
)
async def test_location_mutations_reject_unrelated_or_unauthenticated_senders(
    location_app, attack
) -> None:
    app = location_app
    await dispatch(app, 1, "REPORT hazard near road")
    incident = await app.incidents.by_ref(1)
    sender, key, direct, packet = REPORTER, KEY, True, 2
    if attack == "other_member":
        sender, key = "!00000002", b"\x02" * 32
        await verified_member(app, sender, key)
    elif attack == "plaintext":
        key = None
    elif attack == "broadcast":
        direct = False
    elif attack == "wrong_key":
        key = b"\x03" * 32
    elif attack == "unreviewed":
        await app.database.write(
            "UPDATE member SET pki_state='pending' WHERE mesh_id=?", (REPORTER,)
        )
    else:
        assert (await dispatch(app, packet, "UPD 1 North gate")).kind == ResponseKind.ACK
        incident = await app.incidents.by_ref(1)
    before = await app.incidents.provenance(incident.id)
    result = await app.router.dispatch(
        inbound(packet, "UPD 1 Unsafe replacement", sender, key=key, direct=direct)
    )
    assert result.kind == ResponseKind.ERROR
    assert await app.incidents.by_ref(1) == incident
    assert await app.incidents.provenance(incident.id) == before


@pytest.mark.parametrize(
    "text", ["", " ", "UPD", "0 place", "-1 place", "² place", "9999999999999999999999999 place"]
)
async def test_router_location_syntax_is_bounded(location_app, text) -> None:
    result = await dispatch(location_app, 1, f"UPD {text}")
    assert result.kind == ResponseKind.ERROR
    assert "Oops" not in render_response(result)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "x\ny",
        "\x00",
        "木" * 54,
        "木" * 67,
        "40 -79",
        "-wp shelter",
        "-share 91 0",
        "-share 40 -181",
        "-share nan inf",
        "-share -wp absent",
        "-nopos 40 -79",
        "-share",
        "-share place",
        "-share -nopos",
    ],
)
async def test_invalid_location_does_not_change_record_or_history(location_app, text) -> None:
    app = location_app
    created, _ = await app.incidents.create("road blockage", None)
    before = await app.incidents.provenance(created.id)
    with pytest.raises(ValueError):
        await app.incidents.operator_location(created.id, text, actor="web:operator")
    assert await app.incidents.by_id(created.id) == created
    assert await app.incidents.provenance(created.id) == before
    assert await app.incidents.updates(created.id) == []


async def test_suppression_requires_explicit_resharing_and_places_clear_old_coordinates(
    location_app,
) -> None:
    app = location_app
    await dispatch(app, 1, "REPORT -nopos hazard near road")
    first = await app.incidents.by_ref(1)
    assert first.position_suppressed
    assert (await dispatch(app, 2, "UPD 1 North gate")).kind == ResponseKind.ACK
    assert (await app.incidents.by_ref(1)).position_suppressed
    assert (await dispatch(app, 3, "UPD 1 40,-79")).kind == ResponseKind.ERROR
    assert (await dispatch(app, 4, "UPD 1 -share 40,-79")).kind == ResponseKind.ACK
    value = await app.incidents.by_ref(1)
    assert (value.lat, value.lon, value.position_suppressed) == (40, -79, 0)
    assert value.updated_at > first.updated_at
    # A place replaces the old position; a retry of the same public input is a no-op.
    await app.incidents.operator_location(value.id, "Gate B", actor="web:test")
    await app.incidents.operator_location(value.id, "Gate B", actor="web:test")
    assert len(await app.incidents.updates(value.id, 100)) == 3
    value = await app.incidents.operator_location(value.id, "-nopos", actor="web:test")
    assert value.lat is None and value.position_suppressed
    assert value.location_text == "Location withheld"


@pytest.mark.parametrize("terminal", ["resolved", "false_alarm", "expired", "purged", "merged"])
async def test_stale_or_merged_reference_never_redirects_correction(location_app, terminal) -> None:
    app = location_app
    await dispatch(app, 1, "REPORT road blocked at bridge 40 -79")
    old = await app.incidents.by_ref(1)
    second, _ = await app.incidents.create(
        "road blocked near bridge 40.0001 -79.0001", None, force=True
    )
    if terminal == "merged":
        await app.incidents.merge(old.id, second.id, "web:test")
    else:
        await app.incidents.operator_patch(
            old.id,
            status="resolved" if terminal == "purged" else terminal,
            severity=None,
            resolution="Test ended",
            actor="web:test",
        )
        if terminal == "purged":
            # Remove content via the production retention lifecycle below its prerequisites.
            app.clock.advance(40 * 86_400)
            await app.maintenance.run()
            assert await app.incidents.by_id(old.id) is None
    before = await app.incidents.by_id(second.id)
    result = await dispatch(app, 2, "UPD 1 Wrong place")
    assert result.kind == ResponseKind.ERROR
    assert await app.incidents.by_id(second.id) == before


@pytest.mark.parametrize("boundary", range(1, 6))
@pytest.mark.parametrize("cancel", [False, True])
async def test_correction_rolls_back_each_write_and_recovers_after_reopen(
    location_app, monkeypatch, boundary, cancel
) -> None:
    app = location_app
    created, _ = await app.incidents.create("road blocked", None)
    before = await app.incidents.provenance(created.id)
    origins = await app.incidents.origins(created.id)
    with monkeypatch.context() as patch:
        interrupt_after_write(patch, app.database, boundary, cancel)
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await asyncio.create_task(
                app.incidents.operator_location(created.id, "New place", actor="web:test")
            )
    await app.database.close()
    reopened = Database(app.database.path)
    await reopened.open()
    service = IncidentService(reopened, app.clock, app.incidents.origin_node)
    try:
        assert await service.by_id(created.id) == created
        assert await service.provenance(created.id) == before
        assert await service.origins(created.id) == origins
        assert await service.updates(created.id) == []
        assert await reopened.read("SELECT * FROM audit_log WHERE action='incident.location'") == []
        assert (
            await service.operator_location(created.id, "New place", actor="web:test")
        ).location_text == "New place"
    finally:
        await reopened.close()


async def test_concurrent_corrections_keep_ordered_before_after_evidence(location_app) -> None:
    app = location_app
    created, _ = await app.incidents.create("road blocked", None)
    await asyncio.gather(
        *(
            app.incidents.operator_location(created.id, f"Gate {i}", actor=f"web:{i}")
            for i in range(12)
        )
    )
    events = [
        event
        for event in await app.incidents.provenance(created.id)
        if event["event_kind"] == "location_corrected"
    ]
    assert len(events) == 12
    assert len({event["source_updated_at"] for event in events}) == 12
    for prior, following in zip(events, events[1:], strict=False):
        assert prior["payload"]["after"] == following["payload"]["before"]
    assert [row["seq"] for row in await app.incidents.updates(created.id, 100)] == list(
        range(12, 0, -1)
    )


async def test_waypoint_consent_label_bounds_and_member_preferences(location_app) -> None:
    app = location_app
    member = await app.router.members.resolve(REPORTER)
    await app.database.write(
        "INSERT INTO waypoint(name,slug,latitude,longitude,created_at,updated_at) "
        "VALUES('Community shelter','shelter',40,-79,0,0)"
    )
    # Member privacy is independent from explicitly published incident locations.
    await app.database.write("UPDATE member SET trust='member' WHERE id=?", (member.id,))
    assert (await dispatch(app, 1, "POS SHARE off")).kind == ResponseKind.ACK
    before = [
        tuple(row)
        for row in await app.database.read("SELECT * FROM member WHERE id=?", (member.id,))
    ]
    created, _ = await app.incidents.create("road blocked -nopos", member)
    with pytest.raises(ValueError, match="Coordinates are public"):
        await app.incidents.update_location(created.local_ref, member, "-wp shelter")
    value = await app.incidents.update_location(created.local_ref, member, "-share -wp SHELTER")
    assert (value.lat, value.lon, value.location_text, value.position_suppressed) == (
        40,
        -79,
        "Community shelter",
        0,
    )
    after = [
        tuple(row)
        for row in await app.database.read("SELECT * FROM member WHERE id=?", (member.id,))
    ]
    assert before == after
    await app.database.write("UPDATE waypoint SET name=?", ("木" * 54,))
    with pytest.raises(ValueError, match="Waypoint label"):
        await app.incidents.update_location(created.local_ref, member, "-share -wp shelter")


async def test_revoked_key_is_rechecked_inside_correction_transaction(location_app) -> None:
    app = location_app
    member = await app.router.members.resolve(REPORTER)
    created, _ = await app.incidents.create("road blocked", member)
    await app.database.write("UPDATE member SET pki_state='conflict' WHERE id=?", (member.id,))
    with pytest.raises(ValueError, match="verified PKI"):
        await app.incidents.update_location(created.local_ref, member, "Gate B")
    assert await app.incidents.by_id(created.id) == created


@pytest.mark.parametrize("role", ["operator", "viewer"])
async def test_web_location_requires_session_csrf_role_and_audits_actor(location_app, role) -> None:
    app = location_app
    created, _ = await app.incidents.create("road blocked", None)
    url = f"/api/v1/incidents/{created.id}/location"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.web), base_url="http://test"
    ) as client:
        assert (await client.post(url, json={"location": "North gate"})).status_code == 401
        password = secrets.token_urlsafe(24)
        account = await app.web_auth.create_account(
            "location-test", "Shift operator", role, password, "test"
        )
        await app.database.write(
            "UPDATE web_account SET must_change=0 WHERE id=?", (account["id"],)
        )
        login = await client.post(
            "/api/v1/auth/login", json={"username": "location-test", "password": password}
        )
        assert login.status_code == 200
        assert (await client.post(url, json={"location": "North gate"})).status_code == 403
        client.headers["x-csrf-token"] = login.json()["csrf_token"]
        response = await client.post(url, json={"location": "North gate"})
        assert response.status_code == (200 if role == "operator" else 403)
        if role == "operator":
            assert response.json()["location_text"] == "North gate"
            event = (await app.incidents.provenance(created.id))[-1]
            assert "location-test" in event["actor"]
            rows = await app.database.read(
                "SELECT actor_ref FROM audit_log WHERE action='incident.location'"
            )
            assert len(rows) == 1 and "location-test" in rows[0]["actor_ref"]
            assert (await client.post(url, json={"location": "-share 99 0"})).status_code == 422
            for reference in (0, -1, 9_223_372_036_854_775_808):
                invalid = await client.post(
                    f"/api/v1/incidents/{reference}/location", json={"location": "North gate"}
                )
                assert invalid.status_code == 422
        else:
            assert await app.incidents.by_id(created.id) == created


async def test_corrected_location_federates_same_uid_and_retains_peer_boundary(
    location_app, tmp_path
) -> None:
    app = location_app
    source = IncidentService(app.database, app.clock, "!remote")
    producer = FederationSyncService(app.database, "!remote")
    peer = await active_incident_peer(app.database)
    created, _ = await source.create("road near bridge 40 -79", None)
    receiver_db = Database(tmp_path / "receiver.db")
    await receiver_db.open()
    try:
        receiver_peer = await active_incident_peer(receiver_db)
        receiver = FederationSyncService(receiver_db, "!local")
        now = created.updated_at

        async def transfer() -> None:
            manifest = await producer.manifest(peer)
            wanted = await receiver.missing([item.json() for item in manifest])
            assert len(wanted) == 1
            items = await producer.export_items(peer, wanted)
            assert len(items) == 1
            assert await receiver.quarantine(receiver_peer, items[0], now)
            inbox = await receiver_db.read(
                "SELECT id FROM fed_inbox_item WHERE uid=?", (items[0]["uid"],)
            )
            await receiver.import_inbox(inbox[0]["id"], "web:test", now)

        await transfer()
        before = (await receiver_db.read("SELECT uid,local_ref FROM incident"))[0]
        corrected = await source.operator_location(created.id, "-share 40.01 -79", actor="web:test")
        assert corrected.updated_at > created.updated_at  # no clock advance needed
        await transfer()
        after = (await receiver_db.read("SELECT uid,local_ref,lat,lon FROM incident"))[0]
        assert (after["uid"], after["local_ref"]) == tuple(before)
        assert (after["lat"], after["lon"]) == (40.01, -79)
        await source.operator_location(created.id, "-share 42 -79", actor="web:test")
        assert await producer.manifest(peer) == []
        assert (
            await producer.export_items(peer, [{"stream": "incidents", "uid": before["uid"]}]) == []
        )
        assert (await receiver_db.read("SELECT lat FROM incident"))[0]["lat"] == 40.01
        app.clock.epoch -= timedelta(hours=6)  # wall time steps; monotonic time does not
        await source.operator_location(created.id, "-nopos", actor="web:test")
        await transfer()  # Existing policy allows reports with no coordinates.
        withheld = (await receiver_db.read("SELECT lat,lon,location_text FROM incident"))[0]
        assert tuple(withheld) == (None, None, "Location withheld")
    finally:
        await receiver_db.close()


async def test_forced_report_hint_and_help_are_actionable_and_bounded(location_app) -> None:
    app = location_app
    report = render_response(await dispatch(app, 1, "REPORT! road blocked"))
    assert "send UPD 1 <where>" in report
    assert chunk_text(report) == [report]
    help_text = render_response(await dispatch(app, 2, "HELP UPD"))
    assert "verified DM" in help_text and "-share" in help_text
    assert len(chunk_text(help_text)) <= 3
    response = render_response(await dispatch(app, 3, "UPD 1 " + "木" * 53))
    assert "✓ INC 1 location" in response
    assert chunk_text(response) == [response]
