from __future__ import annotations

import hashlib
import json
import stat
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.replay import (
    ReplayError,
    ReplayHarness,
    ReplaySelection,
    load_corpus,
    provision_drill_operator,
    redacted_bundle,
    write_private_json,
)
from outpost.replay_cli import _validate_destination_paths
from outpost.situation import BriefingCapability
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.store.message_log import MessageLogRepo
from outpost.transport.models import InboundMessage

FIXTURE = Path(__file__).parents[1] / "fixtures" / "replay" / "basic-v1.json"


def message(
    packet_id: int,
    text: str | None,
    at: datetime,
    *,
    payload: bytes | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> InboundMessage:
    return InboundMessage(
        packet_id=packet_id,
        from_id="!10000001",
        to_id="!20000002",
        channel=2,
        portnum=1,
        is_direct=True,
        text=text,
        payload=payload,
        rx_time=at,
        rx_snr=8.5,
        rx_rssi=-91,
        hops_away=2,
        want_ack=True,
        pki_encrypted=True,
        pki_public_key=b"k" * 32,
        via_mqtt=True,
        no_reply=False,
        request_id=44,
        routing_error="NONE",
        latitude=latitude,
        longitude=longitude,
    )


@pytest.mark.asyncio
async def test_message_log_retains_every_inbound_field_needed_for_replay(tmp_path) -> None:
    database = Database(tmp_path / "source.db")
    await database.open()
    clock = VirtualClock(epoch=datetime(2026, 8, 30, 12, tzinfo=UTC))
    inbound = message(
        77,
        None,
        datetime(2026, 8, 30, 11, 59, tzinfo=UTC),
        payload=b"\x01\x02payload",
        latitude=40.4406,
        longitude=-79.9959,
    )
    try:
        await MessageLogRepo(database, clock).record_inbound(inbound)
        rows = await database.read(
            "SELECT to_mesh_id,payload,want_ack,pki_encrypted,pki_public_key,no_reply,"
            "request_id,routing_error,latitude,longitude,rx_time,transport FROM message_log"
        )
        assert dict(rows[0]) == {
            "to_mesh_id": "!20000002",
            "payload": b"\x01\x02payload",
            "want_ack": 1,
            "pki_encrypted": 1,
            "pki_public_key": b"k" * 32,
            "no_reply": 0,
            "request_id": 44,
            "routing_error": "NONE",
            "latitude": 40.4406,
            "longitude": -79.9959,
            "rx_time": int(inbound.rx_time.timestamp()),
            "transport": "mqtt",
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_database_replay_is_deterministic_and_never_mutates_source(tmp_path) -> None:
    source = tmp_path / "source.db"
    database = Database(source)
    await database.open()
    clock = VirtualClock(epoch=datetime(2026, 8, 30, 12, tzinfo=UTC))
    members = MemberRepo(database, clock)
    await members.resolve("!10000001")
    await database.write(
        "UPDATE member SET handle='field-one',trust='member' WHERE mesh_id='!10000001'"
    )
    repo = MessageLogRepo(database, clock)
    await repo.record_inbound(message(10, "PING", clock.now()))
    clock.advance(10)
    await repo.record_inbound(message(11, "BLORP", clock.now()))
    await database.close()
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

    corpus = load_corpus(source, ReplaySelection(limit=10))
    assert [record.text for record in corpus.records] == ["PING", "BLORP"]
    reports = []
    for index in range(2):
        harness = ReplayHarness(
            Config(), corpus, tmp_path / f"scratch-{index}.db", preset="LONG_FAST", region="US"
        )
        try:
            reports.append(await harness.run())
        finally:
            await harness.close()

    assert reports[0] == reports[1]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert reports[0]["summary"] == {
        "processed": 2,
        "transmissions": 2,
        "decisions": {"allowed": 1, "unknown_command": 1},
        "queued_after_replay": 0,
    }
    first, second = reports[0]["messages"]
    assert first["command"] == {"input": "PING", "resolved": "PING", "resolution": None}
    assert first["trust"] == {"level": "member", "decision": "allowed"}
    assert first["response"]["text"] == "pong 8.5dB 2hop"
    assert first["response"]["admission"] == "admitted"
    assert first["transmissions"][0]["destination"] == "!10000001"
    assert second["command"]["resolved"] is None
    assert second["trust"]["decision"] == "unknown_command"


def test_export_pseudonymises_identity_coarsens_position_and_strips_secrets(tmp_path) -> None:
    corpus = load_corpus(FIXTURE)
    positioned = corpus.records[0]
    positioned = positioned.__class__(
        **{
            **positioned.__dict__,
            "payload": b"private binary",
            "pki_encrypted": True,
            "pki_public_key": b"p" * 32,
            "latitude": 40.4406123,
            "longitude": -79.9959123,
        }
    )
    corpus = corpus.__class__(
        records=(positioned, *corpus.records[1:]),
        members=corpus.members,
        source_kind=corpus.source_kind,
        schema_version=corpus.schema_version,
        limitations=corpus.limitations,
        redacted=corpus.redacted,
    )
    bundle = redacted_bundle(corpus, coarsen_meters=1_000)
    exported = bundle["messages"][0]

    assert exported["peer_mesh_id"] != "!10000001"
    assert exported["text"] == "PING"
    assert exported["payload"] is None
    assert exported["pki_public_key"] is None
    assert (exported["latitude"], exported["longitude"]) != (40.4406123, -79.9959123)

    destination = tmp_path / "bundle.json"
    write_private_json(destination, bundle)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    loaded = load_corpus(destination)
    assert loaded.redacted is True
    assert len(loaded.records) == 2
    with pytest.raises(ReplayError, match="destination exists"):
        write_private_json(destination, bundle)


def test_replay_rejects_unsafe_selection_bundle_and_scratch_artifacts(tmp_path) -> None:
    with pytest.raises(ReplayError, match="start-id must not be negative"):
        ReplaySelection(start_id=-1)

    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["messages"][0]["is_direct"] = "yes"
    invalid_bundle = tmp_path / "invalid.json"
    invalid_bundle.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ReplayError, match="is_direct must be true or false"):
        load_corpus(invalid_bundle)

    corpus = load_corpus(FIXTURE)
    scratch = tmp_path / "scratch.db"
    Path(f"{scratch}-wal").touch()
    with pytest.raises(ReplayError, match="scratch database artifact already exists"):
        ReplayHarness(Config(), corpus, scratch)

    source = tmp_path / "source.db"
    source.touch()
    with pytest.raises(ReplayError, match="output must not replace the replay source"):
        _validate_destination_paths(Namespace(source=source, output=source, scratch_db=None))


@pytest.mark.asyncio
async def test_committed_corpus_runs_in_ci_and_drill_mode_is_publicly_unmistakable(
    tmp_path,
) -> None:
    corpus = load_corpus(FIXTURE)
    harness = ReplayHarness(Config(), corpus, tmp_path / "drill.db", mode="drill")
    try:
        await harness.prepare()
        password = await provision_drill_operator(harness.app)
        assert len(password) >= 12
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app.web), base_url="http://test"
        ) as client:
            runtime = await client.get("/api/v1/runtime")
            assert runtime.status_code == 200
            assert runtime.json() == {
                "mode": "drill",
                "simulated": True,
                "source": "recorded mesh traffic",
                "store": "scratch",
                "transmit": "simulated",
            }
            assert (await client.get("/api/v1/status")).status_code == 401
        report = await harness.run()
        assert [item["command"]["resolved"] for item in report["messages"]] == [
            "PING",
            None,
        ]
        assert report["simulation"] == {
            "clock": "virtual",
            "radio": "simulated",
            "store": "scratch",
            "node_id": "!fffffffe",
            "region": "US",
            "preset": "LONG_FAST",
            "provider_access": False,
        }
        assert report["messages"][0]["response"]["airtime_class"] == "reply"
        assert report["messages"][0]["response"]["drop_reason"] is None
        briefing = await harness.app.situation.snapshot(BriefingCapability.OPERATOR)
        assert briefing["sections"][-1]["items"][0]["title"] == (
            "Drill mode · simulated transmission"
        )
    finally:
        await harness.close()


def test_replay_fixture_is_canonical_json() -> None:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert value["format"] == "outpost-replay/v1"
    assert value["metadata"]["redacted"] is True
