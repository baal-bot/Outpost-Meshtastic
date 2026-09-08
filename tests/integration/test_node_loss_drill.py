"""Loss of an origin: surviving replica, stale encrypted checkpoint and fresh enrolment."""

import asyncio
import json
from contextlib import AsyncExitStack

import httpx
import pytest

from outpost import recovery
from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.store.members import MemberRepo
from outpost.transport.simulated import SimulatedRadioLink
from tests.integration.test_encrypted_recovery import PASSPHRASE, PASSWORD
from tests.integration.test_federation_bundles import bundle_nodes as bundle_nodes
from tests.integration.test_federation_bundles import exported, incident, receive

pytestmark = pytest.mark.production_wiring


async def test_lost_origin_survival_matrix_and_opt_in_resident_reenrolment(bundle_nodes, tmp_path):
    source, survivor = bundle_nodes
    original = await incident(source, "hazard synthetic checkpoint crossing")
    residents = MemberRepo(source.app.database, source.app.clock)
    resident = await residents.resolve("!12345678", authenticated_pki_key=b"r" * 32)
    await source.app.database.write(
        "UPDATE member SET trust='member',public_key=pending_public_key,"
        "pending_public_key=NULL,pki_state='verified' WHERE id=?",
        (resident.id,),
    )
    await residents.claim_handle(resident.mesh_id, "synthetic-resident")
    await source.app.checkins.checkin(resident, "ok", "private checkpoint welfare")
    await source.app.database.write(
        "INSERT INTO mail(uid,from_id,from_label,to_id,to_label,body,created_at,expires_at) "
        "VALUES('loss-private',?,'synthetic',?,'synthetic',"
        "'private checkpoint mail',1,253402300799)",
        (resident.id, resident.id),
    )
    await source.app.database.write("UPDATE fed_peer SET tx_counter=21,rx_counter=40")
    await source.app.web_auth.create_account(
        "recovery-reviewer", "Synthetic recovery reviewer", "operator", PASSWORD, "test:loss"
    )
    await source.app.database.write("UPDATE web_account SET must_change=0")
    intents = tmp_path / "loss-intents.yaml"
    intents.write_text("[]\n")
    source.app.config.router.intents_file = str(intents)
    archive = tmp_path / "off-device.opr"
    await asyncio.to_thread(recovery.export, source.app.config, archive, PASSPHRASE)
    # Later public history can reach a separately reviewed replica; later private
    # writes and a still-unsent incident cannot magically exist in either copy.
    public_later = await incident(source, "hazard synthetic replicated after checkpoint")
    await source.app.checkins.checkin(resident, "evacuated", "private after checkpoint")
    raw, _ = await exported(source)
    await receive(survivor, raw)
    never_copied = await incident(source, "hazard synthetic never copied")
    expected_replica = {source.identity + ":" + uid for uid in (original.uid, public_later.uid)}
    rows = await survivor.app.database.read("SELECT origin_uid FROM incident_origin")
    assert {row["origin_uid"] for row in rows} == expected_replica
    # No loss drill step mutates a live station: only close this fixture's store.
    await source.app.radio.close()
    await source.app.database.close()
    async with AsyncExitStack() as stack:
        target = tmp_path / "isolated-archive-review"
        await asyncio.to_thread(recovery.restore, archive.read_bytes(), PASSPHRASE, target)
        clock = VirtualClock()
        radio = SimulatedRadioLink(clock, node_id=source.identity)
        restored = OutpostApp(recovery.workbench_config(target), clock=clock, radio=radio)
        await restored.startup()
        stack.push_async_callback(restored.shutdown)
        assert restored.recovery_fence.active and radio.state.value == "down" and not radio.sent
        recovered = await restored.database.read("SELECT uid FROM incident")
        assert {row["uid"] for row in recovered} == {original.uid}
        assert source.identity + ":" + never_copied.uid not in expected_replica
        assert never_copied.uid not in {row["uid"] for row in recovered}
        assert [
            row["note"] for row in await restored.database.read("SELECT note FROM checkin")
        ] == ["private checkpoint welfare"]
        assert len(await restored.database.read("SELECT * FROM mail")) == 1
        saved_peer = (await restored.database.read("SELECT tx_counter,rx_counter FROM fed_peer"))[0]
        assert tuple(saved_peer) == (21, 40)
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restored.web), base_url="http://outpost.test"
            )
        )
        login = await client.post(
            "/api/v1/auth/login", json={"username": "recovery-reviewer", "password": PASSWORD}
        )
        assert login.status_code == 200
        review = await client.get("/api/v1/recovery/review")
        assert review.status_code == 200 and review.json()["counts"]["checkin"] == 1
        assert review.json()["recorded_mesh_id"] == source.identity
        assert (await client.post("/api/v1/alerts", json={})).status_code == 423
        assert (await client.get("/api/v1/federation/peers")).status_code == 423
        # Arrival with the very same authenticated radio key is not membership,
        # handle ownership, web-account continuity, or permission to copy welfare.
        new_residents = MemberRepo(survivor.app.database, survivor.app.clock)
        arrived = await new_residents.resolve(resident.mesh_id, authenticated_pki_key=b"r" * 32)
        assert (
            arrived.trust == "guest" and arrived.pki_state == "pending" and arrived.handle is None
        )
        assert not await survivor.app.database.read("SELECT * FROM checkin")
        assert not await survivor.app.database.read("SELECT * FROM mail WHERE uid='loss-private'")
        assert not await survivor.app.database.read(
            "SELECT * FROM web_account WHERE username='recovery-reviewer'"
        )
        # A new local resident statement is explicit; it is not imported as the
        # old node's missing roster entry or used to close the old event.
        event = await survivor.app.checkins.open_event(
            "Synthetic local arrivals", "all", "test:loss"
        )
        await survivor.app.checkins.checkin(arrived, "ok", "resident voluntarily reports arrival")
        arrived_checkin = (await survivor.app.database.read("SELECT * FROM checkin"))[0]
        assert (
            arrived_checkin["event_id"] == event.id and arrived_checkin["member_id"] == arrived.id
        )
        assert arrived_checkin["note"] == "resident voluntarily reports arrival"
        assert not await survivor.app.database.read("SELECT * FROM fed_peer_successor")
        assert not source.app.radio.sent and not survivor.app.radio.sent and not radio.sent
        print(
            "\nSynthetic node-loss evidence: "
            + json.dumps(
                {
                    "replica_public_incidents": 2,
                    "checkpoint_incidents": 1,
                    "unrecoverable_uncopied_incidents": 1,
                    "checkpoint_private_checkins": 1,
                    "unrecoverable_later_private_checkins": 1,
                    "automatic_private_imports": 0,
                    "restored_transmissions": 0,
                    "fresh_arrival_trust": arrived.trust,
                    "scope": "synthetic stores; not hardware, radio or recovery-time qualification",
                },
                sort_keys=True,
            )
        )
