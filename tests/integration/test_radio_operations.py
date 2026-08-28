import pytest
from fastapi.testclient import TestClient

from outpost.clock import VirtualClock
from outpost.config import AirtimeConfig
from outpost.radio_operations import RadioOperations
from outpost.store import Database
from outpost.store.outbox import OutboxStore
from outpost.transport.governor import AirtimeGovernor
from outpost.web.api import create_web_app


@pytest.mark.asyncio
async def test_operator_send_uses_governor_and_queue_can_be_cancelled(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = AirtimeGovernor(object(), AirtimeConfig(), clock)  # type: ignore[arg-type]
    operations = RadioOperations(database, governor, clock)

    item_id = await operations.send("Road closed", "^all", 0, "bulletin")
    assert (await operations.queue())[0]["id"] == item_id
    assert (await operations.queue())[0]["traffic_class"] == "bulletin"
    assert await operations.cancel(item_id) is True
    assert await operations.queue() == []
    actions = [row["action"] for row in await database.read("SELECT action FROM audit_log")]
    assert actions == ["mesh.send", "queue.cancel"]
    with pytest.raises(ValueError):
        await operations.send("x" * 201, "^all", 0, "bulletin")
    await database.close()


@pytest.mark.asyncio
async def test_outbound_history_api_filters_explains_and_paginates_stably(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock = VirtualClock()
    governor = AirtimeGovernor(
        object(),  # type: ignore[arg-type]
        AirtimeConfig(),
        clock,
        outbox=OutboxStore(database),
    )
    operations = RadioOperations(database, governor, clock, retention_days=21)
    states = (
        "pending",
        "held",
        "sending",
        "awaiting_ack",
        "sent",
        "acked",
        "failed",
        "expired",
        "cancelled",
        "superseded",
        "retracted",
    )

    async def insert_item(index: int) -> None:
        state = states[(index - 1) % len(states)]
        outcome = None
        attempts = 0
        want_ack = state != "sent"
        packet_id = None
        if state == "sent":
            outcome = "not_requested"
        elif state == "acked":
            outcome = "acked"
            packet_id = 10_000 + index
        elif state == "failed":
            failure_kind = ((index - 1) // len(states)) % 4
            if failure_kind == 0:
                outcome = "naked"
                attempts = 1
            elif failure_kind == 1:
                outcome = "rejected"
                attempts = 1
            elif failure_kind == 2:
                attempts = 3
            else:
                attempts = 1
        elif state == "pending" and index > 1:
            attempts = 1
        elif state == "expired" and ((index - 1) // len(states)) % 2 == 0:
            packet_id = 10_000 + index
            attempts = 1
        terminal = state not in {"pending", "held", "sending", "awaiting_ack"}
        await database.write(
            """
            INSERT INTO outbound_work(
              uid,state,text,destination,channel,traffic_class,severity,want_ack,priority,
              created_at,expires_at,dedupe_hash,multipart,attempts,last_attempt_at,
              packet_id,outcome,last_error,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"history-{index}",
                state,
                f"payload {index}",
                "!00000001",
                index % 4,
                "reply",
                "info",
                int(want_ack),
                0,
                1_900_000_000 + index,
                2_100_000_000 + index,
                f"hash-{index}",
                0,
                attempts,
                1_900_000_100 + index if attempts else None,
                packet_id,
                outcome,
                "ConnectionError: radio socket secret must stay private",
                1_900_000_200 + index if terminal else None,
            ),
        )

    for index in range(1, 131):
        await insert_item(index)

    client = TestClient(
        create_web_app(lambda: {"radio": "up"}, database=database, radio_operations=operations)
    )
    first_response = client.get("/api/v1/mesh/queue?state=all&limit=25")
    assert first_response.status_code == 200
    first = first_response.json()
    assert first["total"] == 130
    assert sum(first["counts"].values()) == 130
    assert len(first["items"]) == 25
    assert [item["id"] for item in first["items"]] == list(range(130, 105, -1))
    assert first["next_cursor"] == 106
    assert first["retention_days"] == 21
    assert "last_error" not in str(first)
    assert "socket secret" not in str(first)
    retrying = next(item for item in first["items"] if item["state"] == "pending")
    assert retrying["reason_code"] == "retry_scheduled"
    assert retrying["outcome_at"] is not None

    current = client.get("/api/v1/mesh/queue?limit=100").json()
    current_states = {"pending", "held", "sending", "awaiting_ack", "failed"}
    assert {item["state"] for item in current["items"]}.issubset(current_states)
    assert current["total"] == sum(current["counts"][state] for state in current_states)
    failures = client.get("/api/v1/mesh/queue?state=failed&limit=100").json()
    assert {item["state"] for item in failures["items"]} == {"failed"}
    assert failures["total"] == failures["counts"]["failed"]

    await insert_item(131)
    second = client.get(
        f"/api/v1/mesh/queue?state=all&limit=25&cursor={first['next_cursor']}"
    ).json()
    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert 131 not in second_ids
    assert [item["id"] for item in second["items"]] == list(range(105, 80, -1))

    terminal = client.get("/api/v1/mesh/queue?state=terminal&limit=100").json()
    reasons = {item["reason_code"] for item in terminal["items"]}
    assert {
        "acked",
        "no_ack_requested",
        "radio_nak",
        "local_policy_rejection",
        "retry_exhausted",
        "transport_failure",
        "ack_timeout",
        "expired_before_send",
        "superseded",
        "cancelled",
    }.issubset(reasons)
    assert all(item["outcome_at"] is not None for item in terminal["items"])
    assert client.get("/api/v1/mesh/queue?state=unknown").status_code == 422
    assert client.get("/api/v1/mesh/queue?limit=101").status_code == 422
    await database.close()
