"""Local responsibility via real Outpost services, writer transactions and identities."""

import asyncio
import json
from datetime import timedelta

import pytest

import outpost.watch.responsibility as responsibility_module
from outpost.situation import BriefingCapability
from outpost.store import Transaction
from outpost.store.members import MemberRepo
from outpost.watch.responsibility import (
    IncidentResponsibilityService,
    ResponsibilityActor,
    ResponsibilityConflict,
    ResponsibilityDenied,
)
from tests.integration.test_outage_readiness import appliance as appliance

pytestmark = pytest.mark.production_wiring
KEY = b"1" * 32


@pytest.fixture
async def coordination(appliance):
    app = appliance
    members = []
    for index in (2, 3, 4):
        member = await MemberRepo(app.database, app.clock).resolve(f"!0000000{index}")
        await app.database.write(
            "UPDATE member SET trust=?,handle=?,pki_state='verified',public_key=? WHERE id=?",
            ("operator" if index == 2 else "responder", f"responder{index}", KEY, member.id),
        )
        members.append(ResponsibilityActor(member_id=member.id, public_key=KEY))
    group = await app.database.write(
        "INSERT INTO responder_group(name,created_at,created_by) VALUES('Search team',0,'test')"
    )
    await app.database.write(
        "INSERT INTO responder_group_member(group_id,member_id,added_at,added_by) "
        "VALUES(?,?,0,'test')",
        (group, members[1].member_id),
    )
    incident, _ = await app.incidents.create(
        "hazard fallen tree", None, force=True, operator_label="test"
    )
    return app, incident, members, group


async def decide(ctx, actor, action, **kwargs):
    app, incident, _, _ = ctx
    service = app.incidents.responsibility
    if action == "offer":
        kind = kwargs["target_kind"]
        target = await app.database.read(
            f"SELECT id FROM incident_responsibility_target WHERE {kind}_id=?",  # noqa: S608
            (kwargs["target_ref"],),
        )
        kwargs["target_ref"] = target[0]["id"]
    token = (await service.snapshot(incident.id))["review_token"]
    return await service.apply(incident.id, actor, action, token, **kwargs)


async def owned(ctx):
    _, _, actors, group = ctx
    await decide(
        ctx,
        actors[0],
        "offer",
        target_kind="group",
        target_ref=group,
        next_action="Check road closure",
    )
    return await decide(ctx, actors[1], "accept")


async def test_team_handoff_preserves_owner_until_explicit_acceptance(coordination):
    app, incident, actors, group = coordination
    offer = await decide(
        coordination,
        actors[0],
        "offer",
        target_kind="group",
        target_ref=group,
        next_action="Check road closure",
    )
    assert offer["owner"] is None and offer["acceptance_pending"]
    with pytest.raises(ResponsibilityDenied):
        await decide(coordination, actors[0], "accept")
    accepted = await decide(coordination, actors[1], "accept")
    assert accepted["owner"]["kind"] == "group" and accepted["verification"] == "fresh"
    await decide(coordination, actors[1], "update", next_action="Place barrier at public junction")
    offered = await decide(
        coordination,
        actors[1],
        "offer",
        target_kind="member",
        target_ref=actors[2].member_id,
        next_action="Maintain barrier",
    )
    assert (
        offered["owner"] == accepted["owner"]
        and offered["next_action"] == "Place barrier at public junction"
    )
    handed = await decide(coordination, actors[2], "accept")
    assert handed["owner"]["reference"] == actors[2].member_id
    with pytest.raises(ResponsibilityDenied):
        await decide(coordination, actors[1], "update", next_action="Old team cannot overwrite")
    completed = await decide(coordination, actors[2], "complete")
    assert completed["state"] == "completed" and completed["owner"] is None
    assert (await app.incidents.by_ref(incident.local_ref)).status == "open"
    history = await app.incidents.responsibility.history(incident.id)
    assert [event["action"] for event in history] == [
        "offer",
        "accept",
        "update",
        "offer",
        "accept",
        "complete",
    ]
    assert (
        len(
            await app.database.read(
                "SELECT id FROM audit_log WHERE action LIKE 'incident.responsibility.%'"
            )
        )
        == 6
    )
    assert not app.radio.sent and not await app.database.read("SELECT id FROM outbound_work")


async def test_concurrent_offers_and_replayed_accept_do_not_replace_newer_owner(coordination):
    app, incident, actors, group = coordination
    service = app.incidents.responsibility
    token = (await service.snapshot(incident.id))["review_token"]
    target_ref = (await service.targets("group"))[0]["reference"]
    results = await asyncio.gather(
        *[
            service.apply(
                incident.id,
                actors[0],
                "offer",
                token,
                target_kind="group",
                target_ref=target_ref,
                next_action=text,
            )
            for text in ("North road", "South road")
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, ResponsibilityConflict) for result in results) == 1
    token = (await service.snapshot(incident.id))["review_token"]
    await service.apply(incident.id, actors[1], "accept", token)
    await decide(
        coordination,
        actors[1],
        "offer",
        target_kind="member",
        target_ref=actors[2].member_id,
        next_action="New owner work",
    )
    newer = await decide(coordination, actors[2], "accept")
    with pytest.raises(ResponsibilityConflict):
        await service.apply(incident.id, actors[1], "accept", token)
    assert await service.snapshot(incident.id) == newer


@pytest.mark.parametrize(
    "boundary", ["restart", "wall_forward", "wall_backward", "monotonic_backward", "age"]
)
async def test_owner_survives_time_uncertainty_but_freshness_does_not(coordination, boundary):
    app, incident, actors, _ = coordination
    before = await owned(coordination)
    service = app.incidents.responsibility
    if boundary == "restart":
        service = IncidentResponsibilityService(app.database, app.clock)
    elif boundary == "wall_forward":
        app.clock.epoch += timedelta(hours=1)
    elif boundary == "wall_backward":
        app.clock.epoch -= timedelta(hours=1)
    elif boundary == "monotonic_backward":
        app.clock.value -= 10
    else:
        app.clock.advance(1801)
    after = await service.snapshot(incident.id)
    assert after["owner"] == before["owner"] and after["verification"] == "stale_or_unverified"
    if boundary != "age":
        with pytest.raises(ResponsibilityConflict):
            await service.apply(
                incident.id, actors[1], "update", before["review_token"], next_action="Old view"
            )
    refreshed = await service.apply(
        incident.id, actors[1], "update", after["review_token"], next_action="Verified current task"
    )
    assert refreshed["verification"] == "fresh"


@pytest.mark.parametrize(
    "boundary", ["demoted", "key_conflict", "key_changed", "left_group", "deleted_group"]
)
async def test_current_identity_and_membership_are_checked_inside_writer(coordination, boundary):
    app, incident, actors, group = coordination
    await owned(coordination)
    if boundary == "demoted":
        await app.database.write(
            "UPDATE member SET trust='guest' WHERE id=?", (actors[1].member_id,)
        )
    elif boundary == "key_conflict":
        await app.database.write(
            "UPDATE member SET pki_state='conflict' WHERE id=?", (actors[1].member_id,)
        )
    elif boundary == "key_changed":
        await app.database.write(
            "UPDATE member SET public_key=? WHERE id=?", (b"2" * 32, actors[1].member_id)
        )
    elif boundary == "left_group":
        await app.database.write("DELETE FROM responder_group_member WHERE group_id=?", (group,))
    else:
        await app.database.write("DELETE FROM responder_group WHERE id=?", (group,))
        await app.database.write(
            "INSERT INTO responder_group(id,name,created_at,created_by) "
            "VALUES(?,'Replacement',0,'test')",
            (group,),
        )
    with pytest.raises(ResponsibilityDenied):
        await decide(coordination, actors[1], "update", next_action="No longer authorized")
    view = await app.incidents.responsibility.snapshot(incident.id)
    assert view["owner"] is not None
    if boundary != "key_changed":
        assert not view["owner"]["available"]
    released = await decide(coordination, actors[0], "release")
    assert released["state"] == "released"


@pytest.mark.parametrize("failure", ["state", "history", "audit"])
async def test_responsibility_history_and_audit_rollback_as_one(coordination, monkeypatch, failure):
    app, incident, actors, group = coordination
    original = Transaction.write
    targets_before = [
        dict(row) for row in await app.database.read("SELECT * FROM incident_responsibility_target")
    ]
    needle = {
        "state": "INSERT INTO incident_responsibility(",
        "history": "INSERT INTO incident_responsibility_event(",
        "audit": "INSERT INTO audit_log",
    }[failure]

    async def fault(self, sql, params=()):
        if needle in sql:
            raise RuntimeError("injected writer failure")
        return await original(self, sql, params)

    monkeypatch.setattr(Transaction, "write", fault)
    with pytest.raises(RuntimeError):
        await decide(
            coordination,
            actors[0],
            "offer",
            target_kind="group",
            target_ref=group,
            next_action="Synthetic assignment",
        )
    for table in (
        "incident_responsibility",
        "incident_responsibility_event",
    ):
        assert not await app.database.read(f"SELECT 1 FROM {table}")  # noqa: S608 - test constant
    assert [
        dict(row) for row in await app.database.read("SELECT * FROM incident_responsibility_target")
    ] == targets_before
    assert not await app.database.read(
        "SELECT id FROM audit_log WHERE action LIKE 'incident.responsibility.%'"
    )


async def test_ack_cancel_and_incident_resolution_do_not_complete_work(coordination):
    app, incident, actors, _ = coordination
    before = await owned(coordination)
    await app.incidents.operator_update(incident.id, "ack", "received", actor="web:test")
    assert (await app.incidents.responsibility.snapshot(incident.id))["owner"] == before["owner"]
    await decide(
        coordination,
        actors[1],
        "offer",
        target_kind="member",
        target_ref=actors[2].member_id,
        next_action="Handoff",
    )
    cancelled = await decide(coordination, actors[0], "cancel")
    assert cancelled["owner"] == before["owner"] and not cancelled["acceptance_pending"]
    await app.incidents.operator_patch(
        incident.id,
        status="resolved",
        severity=None,
        resolution="Incident resolved",
        actor="web:test",
    )
    assert (await app.incidents.responsibility.snapshot(incident.id))["owner"] == before["owner"]
    with pytest.raises(ResponsibilityDenied):
        await decide(coordination, actors[0], "complete")
    assert (await decide(coordination, actors[1], "complete"))["state"] == "completed"


async def test_deleted_recreated_target_cannot_receive_an_old_selection(coordination):
    app, incident, actors, group = coordination
    service = app.incidents.responsibility
    old = (await service.targets("group"))[0]
    token = (await service.snapshot(incident.id))["review_token"]
    await app.database.write("DELETE FROM responder_group WHERE id=?", (group,))
    await app.database.write(
        "INSERT INTO responder_group(id,name,created_at,created_by) "
        "VALUES(?,'Search team',0,'test')",
        (group,),
    )
    await app.database.write(
        "INSERT INTO responder_group_member(group_id,member_id,added_at,added_by) "
        "VALUES(?,?,0,'test')",
        (group, actors[1].member_id),
    )
    current = (await service.targets("group"))[0]
    assert current["label"] == old["label"] and current["reference"] != old["reference"]
    with pytest.raises(ValueError, match="unavailable"):
        await service.apply(
            incident.id,
            actors[0],
            "offer",
            token,
            target_kind="group",
            target_ref=old["reference"],
            next_action="Old target selection",
        )
    assert not await service.history(incident.id)
    await service.apply(
        incident.id,
        actors[0],
        "offer",
        token,
        target_kind="group",
        target_ref=current["reference"],
        next_action="Reviewed replacement team",
    )


async def test_target_pages_validation_and_denied_decisions_have_no_effect(coordination):
    app, incident, actors, _ = coordination
    service = app.incidents.responsibility
    target = (await service.targets("group"))[0]
    assert await service.targets("group", after=target["reference"]) == []
    assert await service.targets("group", query="absent") == []
    assert await service.targets("group", query="TEAM") == [target]
    for options in ({"limit": 0}, {"after": -1}, {"after": 2**63}, {"query": "x" * 51}):
        with pytest.raises(ValueError):
            await service.targets("group", **options)
    for actor in (
        ResponsibilityActor(),
        ResponsibilityActor(member_id=actors[0].member_id, account_id=1),
        ResponsibilityActor(member_id=999, public_key=KEY),
        ResponsibilityActor(account_id=999),
    ):
        with pytest.raises(ResponsibilityDenied):
            await service.snapshot(incident.id, actor)
    token = (await service.snapshot(incident.id))["review_token"]
    invalid = [
        ("absent", {}),
        ("offer", {}),
        ("offer", {"target_kind": "group", "target_ref": target["reference"], "next_action": ""}),
        ("offer", {"target_kind": "group", "target_ref": 0, "next_action": "Bad target"}),
        ("offer", {"target_kind": "group", "target_ref": 9999, "next_action": "Missing target"}),
        (
            "offer",
            {
                "target_kind": "member",
                "target_ref": target["reference"],
                "next_action": "Wrong kind",
            },
        ),
        ("update", {"next_action": "x" * 161}),
        ("update", {"next_action": "two\nlines"}),
        ("update", {"next_action": "No current owner"}),
        ("update", {}),
        ("cancel", {}),
        ("release", {}),
        ("complete", {}),
        ("accept", {"target_kind": "group"}),
        ("cancel", {"next_action": "Unexpected"}),
    ]
    for action, values in invalid:
        with pytest.raises(ValueError):
            await service.apply(incident.id, actors[0], action, token, **values)
    with pytest.raises(ResponsibilityConflict):
        await service.apply(incident.id, actors[0], "cancel", "broken")
    assert not await service.history(incident.id)


async def test_after_commit_cancellation_has_one_durable_decision_and_replay_conflicts(
    coordination, monkeypatch
):
    app, incident, actors, _ = coordination
    service = app.incidents.responsibility
    token = (await service.snapshot(incident.id))["review_token"]
    target = (await service.targets("group"))[0]["reference"]
    original = responsibility_module.write_audit

    async def cancel_after_commit(tx, **kwargs):
        result = await original(tx, **kwargs)
        requester = asyncio.current_task()

        def cancel():
            requester.cancel()

        tx.after_commit(cancel)
        return result

    monkeypatch.setattr(responsibility_module, "write_audit", cancel_after_commit)
    pending = asyncio.create_task(
        service.apply(
            incident.id,
            actors[0],
            "offer",
            token,
            target_kind="group",
            target_ref=target,
            next_action="Committed once",
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await pending
    monkeypatch.setattr(responsibility_module, "write_audit", original)
    assert len(await service.history(incident.id)) == 1
    with pytest.raises(ResponsibilityConflict):
        await service.apply(
            incident.id,
            actors[0],
            "offer",
            token,
            target_kind="group",
            target_ref=target,
            next_action="Committed once",
        )


async def test_reports_briefings_and_member_removal_respect_responsibility_privacy(coordination):
    app, incident, actors, _ = coordination
    await owned(coordination)
    private = "<img src=x> confidential next action 40.44061,-79.99591 !00000003"
    await decide(coordination, actors[1], "update", next_action=private)
    await app.incidents.operator_patch(
        incident.id, status=None, severity="urgent", resolution=None, actor="web:test"
    )
    report = await app.incident_reports.build(incident.id)
    assert report["responsibility"]["owner"] == "Search team"
    assert report["responsibility"]["verification"] == "fresh"
    for exported in (
        json.dumps(report),
        app.incident_reports.csv_export(report),
        app.incident_reports.offline_html(report),
    ):
        assert "Search team" in exported and "Local responsibility" in exported
        assert "40.44061" not in exported and "!00000003" not in exported
        assert "review_token" not in exported and "verification_epoch" not in exported
    assert "<img src=x>" not in app.incident_reports.offline_html(report)
    for capability in (BriefingCapability.PUBLIC, BriefingCapability.MEMBER):
        public = json.dumps(await app.situation.snapshot(capability))
        assert "confidential next action" not in public and "Search team" not in public
    operator = await app.situation.snapshot(BriefingCapability.OPERATOR)
    assert "Search team" in json.dumps(operator) and "confidential next action" in json.dumps(
        operator
    )

    class CaptureNarrator:
        async def narrate_situation(self, snapshot, required_refs):
            assert "confidential next action" not in json.dumps(snapshot)
            assert "Search team" not in json.dumps(snapshot)
            return None, "synthetic-disabled"

    app.situation.narrator = CaptureNarrator()
    await app.situation.snapshot(BriefingCapability.OPERATOR, include_ai=True)
    member = await MemberRepo(app.database, app.clock).resolve("!00000003")
    assert (await app.member_data.summary(member))["incidents"] >= 2
    request, _ = await app.member_data.request_removal(member)
    await app.member_data.review(request["id"], "approve", "Synthetic member removal", "web:test")
    current = await app.incidents.responsibility.snapshot(incident.id)
    assert current["owner"] is not None and not current["owner"]["available"]
    assert current["next_action"] == "" and current["verification"] == "stale_or_unverified"
    history = json.dumps(await app.incidents.responsibility.history(incident.id))
    assert "confidential next action" not in history and "responder3" not in history


async def test_active_responsibility_prevents_a_silent_incident_merge(coordination):
    app, incident, actors, _ = coordination
    await app.incidents.operator_location(incident.id, "-share 40.4406 -79.9959", actor="web:test")
    other, _ = await app.incidents.create(
        "hazard fallen tree 40.4406 -79.9959", None, force=True, operator_label="test"
    )
    await owned(coordination)
    with pytest.raises(ValueError, match="responsibility"):
        await app.incidents.merge(incident.id, other.id, "web:test")
    await decide(coordination, actors[0], "release")
    await app.incidents.merge(incident.id, other.id, "web:test")
    token = (await app.incidents.responsibility.snapshot(incident.id))["review_token"]
    with pytest.raises(ResponsibilityConflict, match="merged"):
        await app.incidents.responsibility.apply(incident.id, actors[0], "cancel", token)
