import asyncio
from datetime import UTC, datetime

import pytest

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.router.models import CommandSpec, Response, ResponseKind, TrustLevel
from outpost.transport.models import InboundMessage, TrafficClass


def inbound(
    packet_id: int,
    text: str | None,
    sender: str,
    *,
    portnum: int = 1,
) -> InboundMessage:
    return InboundMessage(
        packet_id=packet_id,
        from_id=sender,
        to_id="!outpost",
        channel=0,
        portnum=portnum,
        is_direct=True,
        text=text,
        payload=None,
        rx_time=datetime.now(UTC),
    )


def test_safety_classifier_covers_ack_position_and_safety_commands(tmp_path) -> None:
    app = OutpostApp(Config.model_validate({"store": {"path": str(tmp_path / "outpost.db")}}))
    ack = inbound(1, None, "!member", portnum=5)
    ack = InboundMessage(**{**ack.__dict__, "request_id": 99})
    position = inbound(2, None, "!member", portnum=3)
    position = InboundMessage(**{**position.__dict__, "latitude": 40.4, "longitude": -80.0})

    assert app._is_safety_inbound(ack)
    assert app._is_safety_inbound(position)
    assert app._is_safety_inbound(inbound(3, "!REPORT road blocked", "!member"))
    assert app._is_safety_inbound(inbound(4, "OK safe", "!member"))
    assert not app._is_safety_inbound(inbound(5, "WX", "!member"))


@pytest.mark.asyncio
async def test_unordered_worker_dispatch_retains_command_timeout(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "router": {"member_lock_timeout_s": 0.01},
        }
    )
    app = OutpostApp(config)
    await app.database.open()

    async def never_returns(context) -> Response:
        await asyncio.Event().wait()
        return Response(ResponseKind.DETAIL)

    app.router.registry.register(
        CommandSpec(
            name="SLOW",
            aliases=(),
            module="test",
            min_trust=TrustLevel.GUEST,
            airtime_class=TrafficClass.REPLY,
            max_parts=1,
            rate_key="commands",
            help_short="test timeout",
            mutates=False,
            handler=never_returns,
        )
    )
    try:
        response = await asyncio.wait_for(
            app.router.dispatch(inbound(1, "SLOW", "!00000001"), ordered=False),
            timeout=0.5,
        )
        assert response.kind == ResponseKind.ERROR
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_slow_provider_does_not_starve_safety_federation_or_other_senders(
    tmp_path,
) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "router": {"inbound_workers": 2, "inbound_queue_max": 8},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    federation_done = asyncio.Event()
    ordinary_done = asyncio.Event()
    safety_done = asyncio.Event()
    completed: list[str] = []
    ordered_flags: list[bool] = []

    async def handle(message: InboundMessage, *, ordered: bool = True) -> None:
        ordered_flags.append(ordered)
        if message.text == "WX":
            slow_started.set()
            await release_slow.wait()
        if message.portnum == config.radio.federation_portnum:
            federation_done.set()
        if message.text == "PING":
            ordinary_done.set()
        if message.text == "HELPME":
            safety_done.set()
        completed.append(message.text or "federation")

    app._handle_inbound_message = handle  # type: ignore[method-assign]
    workers = [asyncio.create_task(app._inbound_worker(number)) for number in (1, 2)]
    try:
        slow = inbound(1, "WX", "!member-a")
        await app._route_inbound(slow, await app.message_log.record_inbound(slow))
        await asyncio.wait_for(slow_started.wait(), timeout=1)

        ordinary = inbound(2, "PING", "!member-a")
        await app._route_inbound(ordinary, await app.message_log.record_inbound(ordinary))
        federation = inbound(
            3,
            None,
            "!peer-b",
            portnum=config.radio.federation_portnum,
        )
        await app._route_inbound(federation, await app.message_log.record_inbound(federation))
        await asyncio.wait_for(federation_done.wait(), timeout=1)

        safety = inbound(4, "HELPME", "!member-a")
        await app._route_inbound(safety, await app.message_log.record_inbound(safety))
        assert safety_done.is_set()
        assert not ordinary_done.is_set()

        release_slow.set()
        await asyncio.wait_for(ordinary_done.wait(), timeout=1)

        assert completed.index("WX") < completed.index("PING")
        assert completed.index("federation") < completed.index("WX")
        assert ordered_flags == [False, False, False, False]
        assert app.status()["inbound"]["fast_processed"] == 1
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await app.database.close()


@pytest.mark.asyncio
async def test_full_worker_backlog_drops_and_marks_newest_message(tmp_path) -> None:
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "router": {"inbound_queue_max": 1},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    try:
        first = inbound(10, "PING", "!member-a")
        second = inbound(11, "WX", "!member-b")
        await app._route_inbound(first, await app.message_log.record_inbound(first))
        await app._route_inbound(second, await app.message_log.record_inbound(second))

        status = app.status()["inbound"]
        assert status["backlog"] == 1
        assert status["capacity"] == 1
        assert status["backlog_dropped"] == 1
        rows = await app.database.read(
            "SELECT packet_id,outcome,drop_reason FROM message_log ORDER BY packet_id"
        )
        assert [dict(row) for row in rows] == [
            {"packet_id": 10, "outcome": "received", "drop_reason": None},
            {
                "packet_id": 11,
                "outcome": "dropped",
                "drop_reason": "worker backlog full",
            },
        ]
    finally:
        await app.database.close()
