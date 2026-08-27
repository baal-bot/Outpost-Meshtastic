import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.fed import FederationPeerService, FederationTopologyService
from outpost.store import Database
from outpost.web.api import create_web_app

A = "!aaaaaaaa"
B = "!bbbbbbbb"
C = "!cccccccc"
D = "!dddddddd"


async def topology_node(tmp_path, name: str, mesh_id: str):
    database = Database(tmp_path / f"{name}.db")
    await database.open()
    clock = VirtualClock()
    peers = FederationPeerService(database, clock, mesh_id)
    topology = FederationTopologyService(database, peers, clock)
    return database, clock, peers, topology


async def add_peer(
    database: Database, peers: FederationPeerService, mesh_id: str, state: str = "active"
) -> None:
    await peers.discover(mesh_id, f"Node {mesh_id[-1]}", 1, {"bbs": True}, "radio")
    await database.write("UPDATE fed_peer SET state=? WHERE mesh_id=?", (state, mesh_id))


@pytest.mark.asyncio
async def test_location_is_coarse_opt_in_authenticated_and_revocable(tmp_path) -> None:
    a_db, _, a_peers, a_topology = await topology_node(tmp_path, "a", A)
    b_db, _, b_peers, b_topology = await topology_node(tmp_path, "b", B)
    await add_peer(a_db, a_peers, B)
    await add_peer(b_db, b_peers, A)

    disabled = await a_topology.advertisement(B)
    assert disabled["location"] is None
    await a_topology.set_policy(
        B,
        share_location=True,
        location_lat=40.4406,
        location_lon=-79.9959,
        precision_km=10,
        actor="web:operator-a",
    )
    advertisement = await a_topology.advertisement(B)
    assert advertisement["location"] is not None
    assert advertisement["location"]["precision_km"] == 10
    assert advertisement["location"]["lat"] != 40.4406
    await b_topology.accept(A, advertisement)
    peer = (await b_topology.overview())["items"][0]
    assert peer["state"] == "active"
    assert peer["location"]["precision_km"] == 10

    await a_topology.set_policy(
        B,
        share_location=False,
        location_lat=None,
        location_lon=None,
        precision_km=10,
        actor="web:operator-a",
    )
    await b_topology.accept(A, await a_topology.advertisement(B))
    assert (await b_topology.overview())["items"][0]["location"] is None
    with pytest.raises(ValueError, match="location values"):
        await b_topology.accept(
            A,
            {
                "generated_at": int(b_topology.clock.now().timestamp()),
                "location": {"lat": float("nan"), "lon": 0, "precision_km": 10},
            },
        )
    await add_peer(b_db, b_peers, C, "pending")
    with pytest.raises(ValueError, match="active paired peer"):
        await b_topology.accept(C, disabled)
    await a_db.close()
    await b_db.close()


@pytest.mark.asyncio
async def test_topology_health_states_paths_backlog_successor_and_forgotten(tmp_path) -> None:
    database, clock, peers, topology = await topology_node(tmp_path, "health", A)
    await add_peer(database, peers, B)
    await add_peer(database, peers, C, "pending")
    await add_peer(database, peers, D, "rejected")
    await peers.forget(D, "web:operator")
    now = int(clock.now().timestamp())
    await database.write(
        "INSERT INTO message_log(direction,peer_mesh_id,channel,portnum,is_direct,byte_len,"
        "airtime_class,outcome,transport,created_at) VALUES('in',?,0,1,0,20,"
        "'federation','accepted','mqtt',?)",
        (B, now),
    )
    await database.write(
        "INSERT INTO fed_service_request(request_id,direction,peer_mesh_id,service,status,"
        "candidate_peers,created_at,updated_at,expires_at) "
        "VALUES('req','out',?,'weather','pending','[]',?,?,?)",
        (B, now, now, now + 600),
    )
    await database.write(
        "INSERT INTO fed_peer_successor(old_mesh_id,successor_peer_id,old_node_name,adopted_at,"
        "adopted_by) SELECT '!eeeeeeee',id,'Old E',?,'web:operator' FROM fed_peer "
        "WHERE mesh_id=?",
        (now, B),
    )

    current = {item["mesh_id"]: item for item in (await topology.overview())["items"]}
    assert current[B]["identity_kind"] == "successor"
    assert current[B]["last_successful_path"] == "mqtt"
    assert current[B]["preferred_path"] == "mqtt"
    assert current[B]["backlog"] == 1
    assert current[C]["state"] == "discovered"
    assert current[D]["state"] == "forgotten"
    assert current["!eeeeeeee"]["state"] == "adopted"
    assert all(item["location"] is None for item in current.values())

    clock.advance(86_401)
    stale = {item["mesh_id"]: item for item in (await topology.overview())["items"]}[B]
    assert stale["state"] == "stale" and stale["degraded"] is True
    await database.close()


@pytest.mark.asyncio
async def test_topology_api_updates_policy_without_exposing_peer_secrets(tmp_path) -> None:
    database, _, peers, topology = await topology_node(tmp_path, "api", A)
    await add_peer(database, peers, B)
    await database.write("UPDATE fed_peer SET shared_secret=? WHERE mesh_id=?", (b"x" * 32, B))
    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database=database,
            federation=peers,
            federation_topology=topology,
        )
    )

    response = client.put(
        f"/api/v1/federation/topology/peers/{B}",
        json={
            "share_location": True,
            "location_lat": 40.4406,
            "location_lon": -79.9959,
            "precision_km": 25,
        },
    )
    assert response.status_code == 200 and response.json()["share_location"] is True
    body = client.get("/api/v1/federation/topology").json()
    assert body["items"][0]["location_policy"]["share_location"] is True
    assert "secret" not in str(body).lower()
    await database.close()
