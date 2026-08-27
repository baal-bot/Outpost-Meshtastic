import sqlite3

import pytest

from outpost.clock import VirtualClock
from outpost.fed import FederationPeerService, FederationSyncService
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.watch.incidents import IncidentService


async def active_incident_peer(database: Database):
    peers = FederationPeerService(database, VirtualClock(), "!local")
    await peers.discover("!remote", "Remote Outpost", 1, {}, "radio")
    await database.write("UPDATE fed_peer SET state='active' WHERE mesh_id='!remote'")
    return await peers.update_sync_policy(
        "!remote",
        boards=[],
        sync_incidents=True,
        incident_lat=40.0,
        incident_lon=-79.0,
        incident_radius_km=25,
        relay_alerts=False,
        quota_items_per_hour=100,
    )


def remote_incident(
    *,
    version: int,
    status: str = "open",
    title: str = "Road blocked near bridge",
    origin_uids: list[str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "uid": "!remote:incident-1",
        "type": "road",
        "severity": "caution",
        "status": status,
        "title": title,
        "body": title,
        "lat": 40.0,
        "lon": -79.0,
        "location_text": "Bridge",
        "radius_m": 500,
        "reporter_label": "Remote operator",
        "origin_node": "!remote",
        "created_at": 10,
        "updated_at": version,
        "expires_at": 500,
        "resolved_at": version if status in {"resolved", "false_alarm"} else None,
        "resolution_note": "Remote reports clear" if status == "resolved" else None,
    }
    if origin_uids is not None:
        payload["origin_uids"] = origin_uids
    return payload


async def import_remote(
    database: Database,
    sync: FederationSyncService,
    peer,
    *,
    version: int,
    digest: str,
    status: str = "open",
    title: str = "Road blocked near bridge",
    now: int,
    origin_uids: list[str] | None = None,
) -> None:
    item = {
        "stream": "incidents",
        "uid": "!remote:incident-1",
        "digest": digest,
        "payload": remote_incident(
            version=version,
            status=status,
            title=title,
            origin_uids=origin_uids,
        ),
    }
    assert await sync.quarantine(peer, item, now)
    inbox = await database.read(
        "SELECT id FROM fed_inbox_item WHERE peer_id=? AND stream='incidents' AND uid=?",
        (peer.id, item["uid"]),
    )
    await sync.import_inbox(int(inbox[0]["id"]), "operator:test", now + 1)


@pytest.mark.asyncio
async def test_bounded_candidates_require_human_merge_and_support_unmerge(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    member = await MemberRepo(database, clock).resolve("!00000001")
    incidents = IncidentService(database, clock, "!local")
    target, _ = await incidents.create("road closure at bridge 40.0000 -79.0000", member)
    source, _ = await incidents.create(
        "road blocked at bridge 40.0005 -79.0005", member, force=True
    )
    unrelated, _ = await incidents.create("fire at bridge 40.0005 -79.0005", member, force=True)
    far, _ = await incidents.create("road closure at bridge 41.0000 -79.0000", member, force=True)
    assert target and source and unrelated and far
    await incidents.operator_update(target.id, "ack", actor="operator:target")
    await database.write(
        "UPDATE incident SET severity='urgent',expires_at=expires_at+3600 WHERE id=?", (source.id,)
    )

    candidates = await incidents.match_candidates(source.id)

    assert [candidate["id"] for candidate in candidates] == [target.id]
    assert all(len(candidate["reasons"]) == 4 for candidate in candidates)
    before_events = len(await incidents.provenance(target.id))
    merged = await incidents.merge(source.id, target.id, "operator:merge")
    assert merged.status == "monitoring"
    assert merged.severity == "urgent"
    assert merged.expires_at == (await incidents.by_id(source.id)).expires_at
    hidden = await incidents.by_id(source.id)
    assert hidden is not None and hidden.uid == source.uid
    assert hidden.merged_into_id == target.id
    assert {value["origin_uid"] for value in await incidents.origins(target.id)} == {
        source.uid,
        target.uid,
    }
    assert len(await incidents.provenance(target.id)) > before_events
    assert source.id not in {value.id for value in await incidents.list()}
    provenance_id = (await incidents.provenance(target.id))[0]["id"]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        await database.write(
            "UPDATE incident_provenance SET actor='rewritten' WHERE id=?", (provenance_id,)
        )

    restored = await incidents.unmerge(source.id, "operator:unmerge")
    assert restored.uid == source.uid and restored.merged_into_id is None
    assert [value["origin_uid"] for value in await incidents.origins(source.id)] == [source.uid]
    corrected = await incidents.operator_patch(
        source.id,
        status="resolved",
        severity=None,
        resolution="Separate obstruction cleared",
        actor="operator:correct",
    )
    assert corrected.status == "resolved"
    assert (await incidents.provenance(source.id))[-1]["event_kind"] == "operator_correction"
    await database.close()


@pytest.mark.asyncio
async def test_returned_local_origin_is_a_proposal_not_an_authoritative_update(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    peer = await active_incident_peer(database)
    clock = VirtualClock()
    incidents = IncidentService(database, clock, "!local")
    local, _ = await incidents.create(
        "road blocked near bridge 40.0000 -79.0000", None, operator_label="operator:local"
    )
    assert local is not None
    await incidents.operator_update(local.id, "ack", actor="operator:local")
    sync = FederationSyncService(database, "!local")
    version = local.updated_at + 100
    payload = remote_incident(
        version=version,
        status="resolved",
        title="Peer proposes that local road is clear",
        origin_uids=[sync.wire_uid(local.uid)],
    )
    payload["uid"] = sync.wire_uid(local.uid)
    item = {
        "stream": "incidents",
        "uid": sync.wire_uid(local.uid),
        "digest": "returned-local-v1",
        "payload": payload,
    }
    assert await sync.quarantine(peer, item, version + 1)
    inbox = await database.read("SELECT id FROM fed_inbox_item WHERE uid=?", (item["uid"],))
    await sync.import_inbox(int(inbox[0]["id"]), "operator:test", version + 2)

    retained = await incidents.by_id(local.id)
    assert retained is not None
    assert retained.status == "monitoring"
    assert retained.title == local.title
    assert retained.reconciliation_review == 1
    assert len(await database.read("SELECT id FROM incident")) == 1
    assert (await incidents.provenance(local.id))[-1]["event_kind"] == "relayed_origin_update"
    await database.close()


@pytest.mark.asyncio
async def test_reconnect_rejects_stale_and_concurrent_updates_and_keeps_monitoring(
    tmp_path,
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    peer = await active_incident_peer(database)
    sync = FederationSyncService(database, "!local")
    await import_remote(database, sync, peer, version=20, digest="v20", now=30)
    row = (await database.read("SELECT id FROM incident WHERE uid='!remote:incident-1'"))[0]
    incident_id = int(row["id"])
    incidents = IncidentService(database, VirtualClock(), "!local")
    await incidents.operator_update(incident_id, "ack", actor="operator:local")

    assert await sync.missing(
        [{"s": "incidents", "u": "!remote:incident-1", "v": 30, "d": "v30"}]
    ) == [{"stream": "incidents", "uid": "!remote:incident-1"}]
    await import_remote(
        database,
        sync,
        peer,
        version=30,
        digest="v30",
        status="resolved",
        title="Remote says road clear",
        now=40,
    )
    monitored = await incidents.by_id(incident_id)
    assert monitored is not None
    assert monitored.status == "monitoring"
    assert monitored.title == "Remote says road clear"
    assert monitored.reconciliation_review == 1

    await import_remote(
        database,
        sync,
        peer,
        version=25,
        digest="stale",
        title="Stale partition copy",
        now=50,
    )
    await import_remote(
        database,
        sync,
        peer,
        version=30,
        digest="concurrent",
        title="Concurrent partition edit",
        now=60,
    )
    after = await incidents.by_id(incident_id)
    assert after is not None and after.title == "Remote says road clear"
    kinds = [event["event_kind"] for event in await incidents.provenance(incident_id)]
    assert "resolution_withheld" in kinds
    assert "stale_update_ignored" in kinds
    assert "concurrent_update_conflict" in kinds
    await database.close()


@pytest.mark.asyncio
async def test_merged_origin_updates_are_advisory_and_identity_is_exported(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    peer = await active_incident_peer(database)
    sync = FederationSyncService(database, "!local")
    await import_remote(
        database,
        sync,
        peer,
        version=20,
        digest="v20",
        now=30,
        origin_uids=["!remote:incident-1", "!upstream:incident-9"],
    )
    rows = await database.read("SELECT id FROM incident WHERE uid='!remote:incident-1'")
    source_id = int(rows[0]["id"])
    incidents = IncidentService(database, VirtualClock(), "!local")
    target, _ = await incidents.create(
        "road blocked near bridge 40.0002 -79.0002", None, operator_label="operator:local"
    )
    assert target is not None
    await database.write(
        "UPDATE incident SET created_at=? WHERE id=?", (target.created_at, source_id)
    )
    await incidents.merge(source_id, target.id, "operator:merge")
    original_target_title = target.title

    await import_remote(
        database,
        sync,
        peer,
        version=40,
        digest="v40",
        status="monitoring",
        title="Remote crew still inspecting",
        now=50,
    )
    canonical = await incidents.by_id(target.id)
    hidden = await incidents.by_id(source_id)
    assert canonical is not None and canonical.title == original_target_title
    assert canonical.reconciliation_review == 1
    assert hidden is not None and hidden.title == "Remote crew still inspecting"
    exported = await sync.export_items(
        peer, [{"stream": "incidents", "uid": sync.wire_uid(target.uid)}]
    )
    assert set(exported[0]["payload"]["origin_uids"]) == {
        sync.wire_uid(target.uid),
        "!remote:incident-1",
        "!upstream:incident-9",
    }
    restored = await incidents.unmerge(source_id, "operator:unmerge")
    assert restored.title == "Remote crew still inspecting"
    await database.close()
