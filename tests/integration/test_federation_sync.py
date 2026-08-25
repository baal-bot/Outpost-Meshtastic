import pytest

from outpost.clock import VirtualClock
from outpost.fed import FederationPeerService, FederationSyncService
from outpost.store import Database


@pytest.mark.asyncio
async def test_manifest_obeys_peer_policy_and_missing_is_bounded(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    peers = FederationPeerService(database, VirtualClock(), "!local")
    await peers.discover("!remote", "Remote", 1, {}, "radio")
    await database.write("UPDATE fed_peer SET state='active' WHERE mesh_id='!remote'")
    peer = await peers.update_sync_policy(
        "!remote",
        boards=["gen"],
        sync_incidents=True,
        relay_alerts=True,
        quota_items_per_hour=20,
    )
    await database.write("UPDATE board SET federated=1 WHERE slug='gen'")
    await database.write(
        "INSERT INTO incident(uid,local_ref,type,severity,title,reporter_label,origin_node,"
        "created_at,updated_at) VALUES('local:inc:1',99,'road','caution','Road closed',"
        "'operator','local',1,2)"
    )
    sync = FederationSyncService(database)

    manifest = await sync.manifest(peer)

    assert [(item.stream, item.uid) for item in manifest] == [("incidents", "local:inc:1")]
    missing = await sync.missing(
        [item.json() for item in manifest] + [{"stream": "alerts", "uid": "remote:alert:1"}]
    )
    assert missing == [{"stream": "alerts", "uid": "remote:alert:1"}]
    assert set(manifest[0].json()) == {"s", "u", "v", "d"}

    exported = await sync.export_items(peer, [{"stream": "incidents", "uid": "local:inc:1"}])
    assert exported[0]["payload"]["title"] == "Road closed"
    remote_item = {
        "stream": "alerts",
        "uid": "remote:alert:1",
        "digest": "abc",
        "payload": {
            "headline": "Storm warning",
            "severity": "urgent",
            "source": "operator",
            "raised_at": 90,
        },
    }
    assert await sync.quarantine(peer, remote_item, 100)
    assert not await sync.quarantine(peer, remote_item, 101)
    inbox = await database.read("SELECT id FROM fed_inbox_item WHERE uid='remote:alert:1'")
    assert await sync.import_inbox(inbox[0]["id"], "operator", 103) == "alerts"
    imported = await database.read(
        "SELECT headline,raised_by FROM alert WHERE uid='remote:alert:1'"
    )
    assert imported[0]["headline"] == "Storm warning"
    assert imported[0]["raised_by"].startswith("federation:")
    with pytest.raises(ValueError, match="outside peer sync policy"):
        await sync.quarantine(
            peer,
            {"stream": "board:private", "uid": "x", "payload": {}},
            102,
        )
    await database.close()


@pytest.mark.asyncio
async def test_export_namespaces_local_ids_with_outpost_identity(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    peers = FederationPeerService(database, VirtualClock(), "!localnode")
    await peers.discover("!remote", "Remote", 1, {}, "radio")
    await database.write("UPDATE fed_peer SET state='active' WHERE mesh_id='!remote'")
    peer = await peers.update_sync_policy(
        "!remote",
        boards=["gen"],
        sync_incidents=False,
        relay_alerts=False,
        quota_items_per_hour=20,
    )
    await database.write("UPDATE board SET federated=1 WHERE slug='gen'")
    thread_id = await database.write(
        "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at) "
        "VALUES('local:1',1,'Test','local',1,1)"
    )
    await database.write(
        "INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,created_at) "
        "VALUES('local:1',?,1,'operator','local','Body',1)",
        (thread_id,),
    )
    sync = FederationSyncService(database, "!localnode")
    assert await sync.approved_thread("gen", "!localnode:local:1")

    manifest = await sync.manifest(peer)
    assert manifest[0].uid == "!localnode:local:1"
    exported = await sync.export_items(peer, [{"stream": "board:gen", "uid": "!localnode:local:1"}])
    assert exported[0]["uid"] == "!localnode:local:1"
    assert exported[0]["payload"]["uid"] == "!localnode:local:1"
    assert exported[0]["payload"]["thread_uid"] == "!localnode:local:1"
    assert (
        await sync.export_items(peer, [{"stream": "board:gen", "uid": "!somewhere-else:local:1"}])
        == []
    )

    remote_thread = await database.write(
        "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at) "
        "VALUES('!remote:local:9',1,'Remote','!remote',2,2)"
    )
    await database.write(
        "INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,created_at) "
        "VALUES('local:2',?,2,'operator','local','Local reply',2)",
        (remote_thread,),
    )
    reply = await sync.export_items(peer, [{"stream": "board:gen", "uid": "!localnode:local:2"}])
    assert reply[0]["payload"]["thread_uid"] == "!remote:local:9"
    await database.close()
