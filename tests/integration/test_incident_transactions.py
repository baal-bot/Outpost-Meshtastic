import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from outpost.app import OutpostApp
from outpost.bbs.service import BBSService
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.router.models import ResponseKind
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.transport.models import InboundMessage
from outpost.transport.simulated import SimulatedRadioLink
from outpost.watch.incidents import IncidentService


@pytest.fixture
async def incident_store(tmp_path: Path) -> AsyncIterator[tuple[Database, VirtualClock]]:
    database = Database(tmp_path / "incidents.db")
    await database.open()
    try:
        yield database, VirtualClock()
    finally:
        await database.close()


async def assert_complete_origins(database: Database, count: int) -> None:
    rows = await database.read("SELECT * FROM incident ORDER BY local_ref")
    assert len(rows) == count
    assert [row["local_ref"] for row in rows] == list(range(1, count + 1))
    assert len({row["uid"] for row in rows}) == count
    references = await database.read("SELECT local_ref,incident_uid FROM incident_reference")
    assert {tuple(row) for row in references} == {(row["local_ref"], row["uid"]) for row in rows}
    origins = await database.read("SELECT * FROM incident_origin ORDER BY incident_id")
    assert len(origins) == count
    events = await database.read(
        "SELECT * FROM incident_provenance WHERE event_kind='created' ORDER BY incident_id"
    )
    assert len(events) == count
    for incident, origin, event in zip(rows, origins, events, strict=True):
        assert origin["incident_id"] == origin["original_incident_id"] == incident["id"]
        assert origin["origin_uid"] == event["origin_uid"] == incident["uid"]
        assert origin["origin_node"] == event["source_node"] == incident["origin_node"]
        assert event["incident_id"] == incident["id"]
        assert event["actor"] == incident["reporter_label"]
        assert json.loads(event["payload_json"])["body"] == incident["body"]


async def test_concurrent_distinct_reports_have_unique_references_and_complete_origins(
    incident_store,
) -> None:
    database, clock = incident_store
    services = [IncidentService(database, clock), IncidentService(database, clock)]
    results = await asyncio.gather(
        *(services[i % 2].create(f"road obstruction at site {i}", None) for i in range(12)),
        return_exceptions=True,
    )
    assert not [result for result in results if isinstance(result, BaseException)], results
    assert all(created is not None and similar is None for created, similar in results)
    await assert_complete_origins(database, 12)


@pytest.mark.parametrize("force", [False, True])
async def test_concurrent_duplicates_preserve_review_and_force_behavior(
    incident_store, force: bool
) -> None:
    database, clock = incident_store
    service = IncidentService(database, clock)
    results = await asyncio.gather(
        *(service.create("road bridge blocked 10.0 20.0", None, force=force) for _ in range(12)),
        return_exceptions=True,
    )
    assert not [result for result in results if isinstance(result, BaseException)], results
    created = [incident for incident, _ in results if incident is not None]
    similar = [incident for _, incident in results if incident is not None]
    assert len(created) == (12 if force else 1)
    if not force:
        assert len(similar) == 11
        assert {incident.id for incident in similar} == {created[0].id}
    await assert_complete_origins(database, len(created))


@pytest.mark.production_wiring
@pytest.mark.parametrize("entrypoint", ["router", "web", "mixed"])
async def test_concurrent_reports_through_real_router_and_authenticated_web(
    tmp_path: Path, entrypoint: str
) -> None:
    clock = VirtualClock()
    config = Config.model_validate(
        {"store": {"path": str(tmp_path / "app.db")}, "modules": {"watch": {"enabled": True}}}
    )
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock))
    await app.database.open()
    try:
        for i in range(4):
            await app.router.members.resolve(f"!{i + 1:08x}")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.web), base_url="http://test"
        ) as client:
            if entrypoint != "router":
                password = secrets.token_urlsafe(24)
                account = await app.web_auth.create_account(
                    "test-operator", "Test Operator", "operator", password, "test"
                )
                await app.database.write(
                    "UPDATE web_account SET must_change=0 WHERE id=?", (account["id"],)
                )
                login = await client.post(
                    "/api/v1/auth/login", json={"username": "test-operator", "password": password}
                )
                assert login.status_code == 200, login.text
                client.headers["x-csrf-token"] = login.json()["csrf_token"]
            work = []
            if entrypoint != "web":
                work.extend(
                    app.router.dispatch(
                        InboundMessage(
                            i + 1,
                            f"!{i + 1:08x}",
                            "!aaaaaaaa",
                            0,
                            1,
                            True,
                            f"REPORT road obstruction at radio site {i} -nopos",
                            None,
                            clock.now(),
                        )
                    )
                    for i in range(4)
                )
            if entrypoint != "router":
                work.extend(
                    client.post(
                        "/api/v1/incidents", json={"text": f"road obstruction web site {i}"}
                    )
                    for i in range(4)
                )
            results = await asyncio.gather(*work, return_exceptions=True)
            assert not [result for result in results if isinstance(result, BaseException)], results
            for result in results:
                if isinstance(result, httpx.Response):
                    assert result.status_code == 200, result.text
                else:
                    assert result.kind == ResponseKind.ACK, result.lines
        await assert_complete_origins(app.database, 8 if entrypoint == "mixed" else 4)
    finally:
        await app.ai_service.close()
        await app.database.close()


def interrupt_after_write(
    monkeypatch: pytest.MonkeyPatch, database: Database, boundary: int, cancellation: bool
) -> None:
    original = database._writer_write
    calls = 0

    async def interrupted(sql: str, params: Sequence[Any] = ()) -> int:
        nonlocal calls
        result = await original(sql, params)
        calls += 1
        if calls == boundary:
            if cancellation:
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                await asyncio.sleep(0)
            raise RuntimeError("injected write interruption")
        return result

    monkeypatch.setattr(database, "_writer_write", interrupted)


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("cancellation", [False, True])
async def test_creation_rolls_back_at_every_write_boundary_and_recovers_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: int, cancellation: bool
) -> None:
    database = Database(tmp_path / "restart.db")
    clock = VirtualClock()
    await database.open()
    service = IncidentService(database, clock)
    try:
        with monkeypatch.context() as patch:
            interrupt_after_write(patch, database, boundary, cancellation)
            error = asyncio.CancelledError if cancellation else RuntimeError
            with pytest.raises(error):
                await asyncio.create_task(service.create("road interrupted report", None))
        assert await database.read("SELECT * FROM incident") == []
        assert await database.read("SELECT * FROM incident_origin") == []
        assert await database.read("SELECT * FROM incident_provenance") == []
        assert await database.read("SELECT * FROM incident_reference") == []
    finally:
        await database.close()
    reopened = Database(database.path)
    await reopened.open()
    try:
        assert await reopened.read("SELECT * FROM incident") == []
        created, _ = await IncidentService(reopened, clock).create("road after interruption", None)
        assert created is not None and created.local_ref == 1
        await assert_complete_origins(reopened, 1)
    finally:
        await reopened.close()


@pytest.mark.parametrize("same_member", [False, True])
async def test_concurrent_reactions_and_operator_updates_have_atomic_counts_and_sequences(
    incident_store, same_member: bool
) -> None:
    database, clock = incident_store
    services = [IncidentService(database, clock), IncidentService(database, clock)]
    members = MemberRepo(database, clock)
    participants = [await members.resolve(f"!{i + 1:08x}") for i in range(12)]
    incident, _ = await services[0].create("road bridge blocked", None)
    assert incident is not None
    work = [
        services[i % 2].react(
            incident.local_ref,
            participants[0 if same_member else i],
            "confirm" if i % 4 == 0 else "dispute",
            f"reaction {i}",
        )
        for i in range(12)
    ]
    work.extend(
        services[i % 2].operator_update(incident.id, "update", f"Operator note {i}")
        for i in range(4)
    )
    work.append(services[0].operator_update(incident.id, "ack"))
    results = await asyncio.gather(*work, return_exceptions=True)
    assert not [result for result in results if isinstance(result, BaseException)], results
    updated = await services[0].by_id(incident.id)
    assert updated is not None and updated.status == "monitoring"
    assert updated.confirm_count == (1 if same_member else 3)
    assert updated.dispute_count == (1 if same_member else 9)
    assert updated.flagged_for_review == int(not same_member)
    count = 7 if same_member else 17
    updates = await database.read("SELECT seq FROM incident_update ORDER BY seq")
    assert [row["seq"] for row in updates] == list(range(1, count + 1))
    assert len(await services[0].provenance(incident.id)) == count + 1


@pytest.mark.parametrize("cancellation", [False, True])
@pytest.mark.parametrize(
    ("operation", "boundary"),
    [
        (operation, boundary)
        for operation in ("confirm", "dispute", "ack", "update", "expire")
        for boundary in (1, 2, 3)
    ]
    + [("patch", 1), ("patch", 2)],
)
async def test_incident_mutations_roll_back_history_counts_and_provenance_together(
    incident_store,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    boundary: int,
    cancellation: bool,
) -> None:
    database, clock = incident_store
    service = IncidentService(database, clock)
    member = await MemberRepo(database, clock).resolve("!00000001")
    incident, _ = await service.create("fire near shed", member)
    assert incident is not None
    before = await service.provenance(incident.id)
    if operation in {"confirm", "dispute"}:
        work = service.react(incident.local_ref, member, operation, "checked")
    elif operation == "patch":
        work = service.operator_patch(
            incident.id, status="resolved", severity=None, resolution="All clear", actor="test"
        )
    elif operation == "expire":
        clock.advance(12 * 3600 + 1)
        work = service.expire_due()
    else:
        work = service.operator_update(incident.id, operation, "Operator note")
    with monkeypatch.context() as patch:
        interrupt_after_write(patch, database, boundary, cancellation)
        error = asyncio.CancelledError if cancellation else RuntimeError
        with pytest.raises(error):
            await asyncio.create_task(work)
    assert await service.by_id(incident.id) == incident
    assert await service.updates(incident.id) == []
    assert await service.provenance(incident.id) == before
    updated = await service.react(incident.local_ref, member, "confirm")
    assert updated.confirm_count == 1
    assert [row["seq"] for row in await service.updates(incident.id)] == [1]


async def test_concurrent_expiry_records_one_transition_per_incident(incident_store) -> None:
    database, clock = incident_store
    services = [IncidentService(database, clock), IncidentService(database, clock)]
    incident, _ = await services[0].create("fire near shed", None)
    assert incident is not None
    clock.advance(12 * 3600 + 1)
    results = await asyncio.gather(
        *(service.expire_due() for service in services), return_exceptions=True
    )
    assert not [result for result in results if isinstance(result, BaseException)], results
    assert [value.id for batch in results for value in batch] == [incident.id]
    updates = await services[0].updates(incident.id)
    assert len(updates) == 1 and updates[0]["kind"] == "status_change"
    assert [row["event_kind"] for row in await services[0].provenance(incident.id)].count(
        "expired"
    ) == 1


async def test_concurrent_bbs_creation_remains_atomic_positive_control(incident_store) -> None:
    database, clock = incident_store
    members = MemberRepo(database, clock)
    member = await members.resolve("!00000001")
    member = await members.claim_handle(member.mesh_id, "sender")
    service = BBSService(database, clock, "local")
    results = await asyncio.gather(
        *(service.create_thread("roads", f"Site {i}", member) for i in range(12)),
        return_exceptions=True,
    )
    assert not [result for result in results if isinstance(result, BaseException)], results
    assert len(await database.read("SELECT id FROM thread")) == 12
