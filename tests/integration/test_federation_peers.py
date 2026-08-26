from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from outpost.bbs.admin import BBSAdmin
from outpost.clock import VirtualClock
from outpost.fed import FederationPeerService
from outpost.store import Database
from outpost.web.api import create_web_app


@pytest.mark.asyncio
async def test_discovery_never_auto_pairs_and_merges_transports(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = FederationPeerService(database, VirtualClock(), "!local")

    peer = await service.discover("!remote", "Remote", 1, {"internet": True}, "radio")
    peer = await service.discover("!remote", "Remote", 1, {"internet": True}, "mqtt")

    assert peer.state == "pending"
    assert peer.discovery_transports == ["mqtt", "radio"]
    assert "shared_secret" not in asdict(peer)
    await database.close()


@pytest.mark.asyncio
async def test_operator_pairing_and_explicit_key_rotation(tmp_path) -> None:
    local_db = Database(tmp_path / "local.db")
    remote_db = Database(tmp_path / "remote.db")
    await local_db.open()
    await remote_db.open()
    local = FederationPeerService(local_db, VirtualClock(), "!local")
    remote = FederationPeerService(remote_db, VirtualClock(), "!remote")
    await local.discover("!remote", "Remote", 1, {}, "radio")
    await remote.discover("!local", "Local", 1, {}, "radio")

    pending, request = await local.create_pairing_request("!remote")
    assert request["target_mesh_id"] == "!remote"
    _, acknowledgement, remote_code = await remote.accept_pairing_request(
        "!local", request["public_key"], request["nonce"]
    )
    assert acknowledgement["target_mesh_id"] == "!local"
    _, local_code = await local.accept_pairing_ack(
        "!remote", acknowledgement["public_key"], acknowledgement["nonce"]
    )
    assert pending.state == "pairing"
    assert local_code == remote_code
    with pytest.raises(ValueError, match="does not match"):
        await local.approve_local(
            "!remote", "operator", "000000" if local_code != "000000" else "999999"
        )
    assert (await local.approve_local("!remote", "operator", local_code)).state == "pairing"
    assert (await remote.approve_local("!local", "operator", remote_code)).state == "pairing"
    assert (await local.confirm_remote("!remote")).state == "active"
    assert (await remote.confirm_remote("!local")).state == "active"
    assert await local.secret("!remote") == await remote.secret("!local")

    with pytest.raises(ValueError, match="explicit key replacement"):
        await local.create_pairing_request("!remote")
    await local.create_pairing_request("!remote", replace=True)
    await local_db.close()
    await remote_db.close()


@pytest.mark.asyncio
async def test_counters_persist_and_replays_are_rejected(tmp_path) -> None:
    path = tmp_path / "outpost.db"
    database = Database(path)
    await database.open()
    service = FederationPeerService(database, VirtualClock(), "!local")
    await service.discover("!remote", "Remote", 1, {}, "radio")
    await database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,local_approved=1,remote_approved=1 "
        "WHERE mesh_id='!remote'",
        (bytes(range(32)),),
    )

    assert await service.next_counter("!remote") == 1
    assert await service.accept_counter("!remote", 4)
    assert not await service.accept_counter("!remote", 4)
    assert not await service.accept_counter("!remote", 3)
    await database.close()

    reopened = Database(path)
    await reopened.open()
    restarted = FederationPeerService(reopened, VirtualClock(), "!local")
    assert not await restarted.accept_counter("!remote", 4)
    assert await restarted.accept_counter("!remote", 5)
    assert await restarted.next_counter("!remote") == 2
    await reopened.close()


@pytest.mark.asyncio
async def test_unpair_returns_peer_to_pending_and_revokes_trust(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = FederationPeerService(database, VirtualClock(), "!local")
    await service.discover("!remote", "Remote", 1, {}, "radio")
    await database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,pairing_nonce=?,"
        "local_approved=1,remote_approved=1,tx_counter=4,rx_counter=7,"
        "approved_by='operator',approved_at=1 WHERE mesh_id='!remote'",
        (bytes(range(32)), bytes(range(16))),
    )

    peer = await service.set_state("!remote", "pending")
    row = (
        await database.read(
            "SELECT shared_secret,pairing_nonce,tx_counter,rx_counter,approved_by,approved_at "
            "FROM fed_peer WHERE mesh_id='!remote'"
        )
    )[0]

    assert peer.state == "pending"
    assert not peer.local_approved and not peer.remote_approved
    assert row["shared_secret"] is None and row["pairing_nonce"] is None
    assert row["tx_counter"] == 0 and row["rx_counter"] == 0
    assert row["approved_by"] is None and row["approved_at"] is None
    await database.close()


@pytest.mark.asyncio
async def test_peer_api_lists_and_operator_rejects_without_exposing_secret(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = FederationPeerService(database, VirtualClock(), "!local")
    await service.discover("!remote", "Remote Outpost", 1, {"weather": True}, "radio")
    client = TestClient(
        create_web_app(lambda: {"radio": "up"}, database=database, federation=service)
    )

    listing = client.get("/api/v1/federation/peers")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["state"] == "pending"
    assert "shared_secret" not in listing.text
    rejected = client.patch("/api/v1/federation/peers/!remote", json={"state": "rejected"})
    assert rejected.status_code == 200 and rejected.json()["state"] == "rejected"
    forgotten = client.delete("/api/v1/federation/peers/!remote")
    assert forgotten.status_code == 200
    assert client.get("/api/v1/federation/peers").json()["items"] == []
    await database.close()


@pytest.mark.asyncio
async def test_sync_policy_requires_pairing_and_is_bounded(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = FederationPeerService(database, VirtualClock(), "!local")
    await service.discover("!remote", "Remote", 1, {}, "radio")
    with pytest.raises(ValueError, match="paired peer"):
        await service.update_sync_policy(
            "!remote",
            boards=["public"],
            sync_incidents=True,
            relay_alerts=True,
            quota_items_per_hour=20,
        )
    await database.write("UPDATE fed_peer SET state='active' WHERE mesh_id='!remote'")
    peer = await service.update_sync_policy(
        "!remote",
        boards=[" Public ", "public", "mutual-aid"],
        sync_incidents=True,
        relay_alerts=True,
        quota_items_per_hour=30,
    )
    assert peer.boards == ["mutual-aid", "public"]
    assert peer.sync_incidents and peer.relay_alerts
    assert peer.quota_items_per_hour == 30
    await database.close()


@pytest.mark.asyncio
async def test_board_federation_toggle_updates_active_peer_policies(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    service = FederationPeerService(database, clock, "!local")
    await service.discover("!remote", "Remote", 1, {}, "radio")
    await database.write("UPDATE fed_peer SET state='active' WHERE mesh_id='!remote'")
    admin = BBSAdmin(database, clock, set())
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database=database,
            bbs_admin=admin,
            federation=service,
        )
    )
    board_id = int((await database.read("SELECT id FROM board WHERE slug='gen'"))[0]["id"])

    enabled = client.patch(f"/api/v1/boards/{board_id}", json={"federated": True})
    assert enabled.status_code == 200
    assert (await service.by_mesh_id("!remote")).boards == ["gen"]

    disabled = client.patch(f"/api/v1/boards/{board_id}", json={"federated": False})
    assert disabled.status_code == 200
    assert (await service.by_mesh_id("!remote")).boards == []
    await database.close()


@pytest.mark.asyncio
async def test_sync_status_reports_transport_and_delivery_health(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    service = FederationPeerService(database, VirtualClock(), "!local")
    peer = await service.discover("!remote", "Remote", 1, {}, "mqtt")
    await database.write("UPDATE fed_peer SET state='active' WHERE id=?", (peer.id,))
    thread_id = await database.write(
        "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at) "
        "VALUES('local:telemetry',1,'Telemetry','local',1,1)"
    )
    post_id = await database.write(
        "INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,created_at) "
        "VALUES('local:telemetry',?,1,'operator','local','Test',1)",
        (thread_id,),
    )
    await database.write(
        "INSERT INTO fed_post_delivery(peer_id,post_id,uid,stream,state,attempts,created_at,"
        "updated_at,delivered_at) VALUES(?,?,?,'board:gen','delivered',3,1,2,2)",
        (peer.id, post_id, "!local:local:telemetry"),
    )
    await database.write(
        "INSERT INTO message_log(direction,peer_mesh_id,channel,portnum,is_direct,byte_len,"
        "airtime_class,outcome,transport,created_at) "
        "VALUES('in','!remote',0,260,0,40,'federation','received','mqtt',unixepoch())"
    )
    await database.write(
        "INSERT INTO message_log(direction,peer_mesh_id,channel,portnum,is_direct,byte_len,"
        "airtime_class,outcome,drop_reason,transport,created_at) "
        "VALUES('in','!remote',0,260,0,40,'federation','rejected','replay detected',"
        "'radio',unixepoch())"
    )
    client = TestClient(
        create_web_app(lambda: {"radio": "up"}, database=database, federation=service)
    )

    result = client.get("/api/v1/federation/sync-status").json()
    transfer = result["items"][0]["transfers"]
    assert transfer["paths"]["mqtt"]["count_24h"] == 1
    assert transfer["paths"]["radio"]["count_24h"] == 0
    assert transfer["deliveries"]["delivered"] == 1
    assert transfer["deliveries"]["retries"] == 2
    assert transfer["deliveries"]["recovered"] == 1
    assert transfer["security"]["rejected_24h"] == 1
    assert transfer["security"]["recent"][0]["reason"] == "replay detected"
    await database.close()
