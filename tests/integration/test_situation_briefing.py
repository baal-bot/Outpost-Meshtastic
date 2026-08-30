from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.render import render_response
from outpost.situation import BriefingCapability, SituationBriefingService
from outpost.store import Database
from outpost.store.members import MemberRepo
from outpost.transport.chunker import chunk_text
from outpost.transport.models import InboundMessage
from outpost.web.api import create_web_app
from outpost.web.auth import WebAuthService


class FixtureNarrator:
    def __init__(self) -> None:
        self.calls = 0

    async def narrate_situation(
        self, snapshot: dict[str, Any], required_refs: tuple[str, ...]
    ) -> tuple[str | None, str]:
        self.calls += 1
        suffix = " stale" if any(source["stale"] for source in snapshot["sources"]) else ""
        return f"Local brief {' '.join(required_refs)}{suffix}".strip(), "answered"


class FailedNarrator:
    async def narrate_situation(
        self, snapshot: dict[str, Any], required_refs: tuple[str, ...]
    ) -> tuple[str | None, str]:
        del snapshot, required_refs
        raise TimeoutError("fixture provider timeout")


async def seeded_service(
    tmp_path,
) -> tuple[Database, VirtualClock, SituationBriefingService, FixtureNarrator]:  # type: ignore[no-untyped-def]
    clock = VirtualClock(epoch=datetime(2026, 8, 28, 12, tzinfo=UTC))
    now = int(clock.now().timestamp())
    database = Database(tmp_path / "outpost.db")
    await database.open()
    members = MemberRepo(database, clock)
    alex = await members.resolve("!00000001")
    await database.write(
        "UPDATE member SET handle='alex',trust='member',notes='private operator note' WHERE id=?",
        (alex.id,),
    )
    incident_id = await database.write(
        """
        INSERT INTO incident(uid,local_ref,type,severity,status,title,body,lat,lon,reporter_label,
          origin_node,created_at,updated_at,source,unverified,dispute_count)
        VALUES('local:one',1,'fire','critical','open','Fire at 40.4406,-79.9959',
          'private incident body',40.4406,-79.9959,'alex','local',?,?, 'member',1,1)
        """,
        (now - 200, now - 200),
    )
    duplicate_id = await database.write(
        """
        INSERT INTO incident(uid,local_ref,type,severity,status,title,reporter_label,origin_node,
          created_at,updated_at,source,merged_into_id)
        VALUES('remote:duplicate',2,'fire','critical','open','Fire duplicate','remote','remote',
          ?,?,'federation',?)
        """,
        (now - 100, now - 100, incident_id),
    )
    assert duplicate_id > incident_id
    for index, (severity, headline, source) in enumerate(
        (
            ("critical", "Evacuate near 40.4406,-79.9959", "operator"),
            ("urgent", "Shelter in place", "incident"),
        ),
        start=1,
    ):
        await database.write(
            """
            INSERT INTO alert(uid,incident_id,severity,headline,source,channels,raised_by,
              raised_at,expires_at,ack_required)
            VALUES(?,?,?,?,?,'[0]','fixture',?,?,1)
            """,
            (
                f"alert:{index}",
                incident_id,
                severity,
                headline,
                source,
                now - 7 * 3600,
                now + 3600,
            ),
        )
    event_id = await database.write(
        "INSERT INTO watch_event(name,opened_at,opened_by,roster_policy) "
        "VALUES('Flood accountability',?,'fixture','all')",
        (now - 300,),
    )
    await database.write(
        "INSERT INTO checkin(member_id,event_id,status,note,lat,lon,created_at) "
        "VALUES(?,?,'need_help','private medical note',40.12345,-80.54321,?)",
        (alex.id, event_id, now - 100),
    )
    protected_board = int((await database.read("SELECT id FROM board WHERE slug='roads'"))[0]["id"])
    public_board = int((await database.read("SELECT id FROM board WHERE slug='gen'"))[0]["id"])
    await database.write(
        "UPDATE board SET min_read_trust='operator' WHERE id=?", (protected_board,)
    )
    await database.write(
        "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at,post_count) "
        "VALUES('protected',?,'SECRET BOARD SUBJECT','local',?,?,1)",
        (protected_board, now - 10, now - 10),
    )
    await database.write(
        "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at,post_count) "
        "VALUES('public',?,'Community water available','local',?,?,1)",
        (public_board, now - 20, now - 20),
    )
    await database.write(
        "INSERT INTO mail(uid,from_label,to_label,body,created_at,state,expires_at) "
        "VALUES('private-mail','alex','operator','PRIVATE MAIL BODY',?,'delivered',?)",
        (now, now + 3600),
    )
    forecast = {
        "daily": [
            {
                "name": "Friday",
                "start_time": "2026-08-28",
                "high_c": 29,
                "low_c": 20,
                "precipitation_probability": 80,
                "wind_kph": 70,
                "summary": "Thunderstorms with hail",
            },
            {
                "name": "Saturday",
                "start_time": "2026-08-29",
                "high_c": 25,
                "low_c": 17,
                "precipitation_probability": 10,
                "wind_kph": 10,
                "summary": "Clear",
            },
        ],
        "hourly": [],
    }
    await database.write(
        "INSERT INTO env_cache(cache_key,provider,payload,fetched_at,expires_at) VALUES(?,?,?,?,?)",
        (
            "forecast:40.4406,-79.9959",
            "nws",
            json.dumps(forecast),
            now - 7 * 3600,
            now - 60,
        ),
    )
    narrator = FixtureNarrator()
    service = SituationBriefingService(
        database,
        clock,
        lambda: {
            "radio": "up",
            "queues": {"reply": 0},
            "inbound": {"backlog": 0},
        },
        narrator=narrator,
    )
    return database, clock, service, narrator


@pytest.mark.asyncio
async def test_snapshot_is_ordered_redacted_role_filtered_and_ai_cached(tmp_path) -> None:
    database, _clock, service, narrator = await seeded_service(tmp_path)
    try:
        await database.write(
            "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<65) "
            "INSERT INTO kv(ns,k,v,expires_at,updated_at) "
            "SELECT 'situation_narration','old:'||n,'{}',9999999999,n FROM seq"
        )
        public = await service.snapshot(BriefingCapability.PUBLIC, include_ai=True)
        rendered = json.dumps(public)
        assert [section["id"] for section in public["sections"]] == [
            "alerts",
            "incidents",
            "welfare",
            "weather",
            "community",
            "delivery",
            "network",
        ]
        assert "40.4406" not in rendered and "-79.9959" not in rendered
        assert "40.12345" not in rendered and "private medical note" not in rendered
        assert "private incident body" not in rendered and "PRIVATE MAIL BODY" not in rendered
        assert "SECRET BOARD SUBJECT" not in rendered
        assert "Community water available" in rendered
        assert "alex" not in rendered
        assert len(public["sections"][1]["items"]) == 1
        assert public["sources"][0]["age_seconds"] >= 0
        assert any(source["conflict"] for source in public["sources"])
        assert public["ai"]["text"] and public["ai"]["outcome"] == "answered"
        cache_size = await database.read(
            "SELECT COUNT(*) count FROM kv WHERE ns='situation_narration'"
        )
        assert int(cache_size[0]["count"]) == 64

        repeated = await service.snapshot(BriefingCapability.PUBLIC, include_ai=True)
        assert repeated["digest"] == public["digest"]
        assert repeated["changes"] == public["changes"] == []
        assert repeated["ai"]["cached"] is True
        assert narrator.calls == 1

        responder = await service.snapshot(BriefingCapability.RESPONDER)
        assert "@alex" in json.dumps(responder)
        assert "private medical note" not in json.dumps(responder)
        assert narrator.calls == 1

        failed_service = SituationBriefingService(
            database,
            service.clock,
            service.status_provider,
            narrator=FailedNarrator(),
        )
        failed = await failed_service.snapshot(BriefingCapability.OPERATOR, include_ai=True)
        assert failed["items"] and failed["ai"]["outcome"] == "provider_error"
        assert failed["ai"]["text"] is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_changes_require_evidence_and_clock_shift_never_creates_resolution(tmp_path) -> None:
    database, clock, service, _narrator = await seeded_service(tmp_path)
    try:
        first = await service.snapshot(BriefingCapability.PUBLIC)
        assert first["changes"] == []
        now = int(clock.now().timestamp())
        await database.write(
            "UPDATE incident SET title='Fire perimeter changed',updated_at=? WHERE uid='local:one'",
            (now + 1,),
        )
        await database.write("DELETE FROM thread WHERE uid='public'")
        changed = await service.snapshot(BriefingCapability.PUBLIC)
        assert {(item["kind"], item["ref"]) for item in changed["changes"]} == {("changed", "I1")}
        assert not any(
            item["kind"] == "resolved" and item["section"] == "community"
            for item in changed["changes"]
        )
        no_change = await service.snapshot(BriefingCapability.PUBLIC)
        assert no_change["changes"] == []
        assert no_change["digest"] != changed["digest"]

        clock.advance(2)
        resolved_at = int(clock.now().timestamp())
        await database.write(
            "UPDATE incident SET status='resolved',resolved_at=?,updated_at=? "
            "WHERE uid='local:one'",
            (resolved_at, resolved_at),
        )
        resolved = await service.snapshot(BriefingCapability.PUBLIC)
        assert any(
            item["kind"] == "resolved" and item["ref"] == "I1" for item in resolved["changes"]
        )

        clock.epoch = datetime(2025, 8, 28, 12, tzinfo=UTC)
        shifted = await service.snapshot(BriefingCapability.PUBLIC)
        assert all(source["age_seconds"] >= 0 for source in shifted["sources"])
        assert not any(item["kind"] == "resolved" for item in shifted["changes"])
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_web_viewers_keep_independent_handover_markers_and_explicit_since(tmp_path) -> None:
    database, clock, service, _narrator = await seeded_service(tmp_path)
    auth = WebAuthService(database, 12)
    accounts = {}
    passwords = {
        "alpha": "alpha-operator-pass-42",
        "bravo": "bravo-operator-pass-42",
        "wallboard": "wallboard-viewer-pass-42",
    }
    try:
        for username, role in (
            ("alpha", "operator"),
            ("bravo", "operator"),
            ("wallboard", "viewer"),
        ):
            accounts[username] = await auth.create_account(
                username,
                username.title(),
                role,
                passwords[username],
                "fixture",
            )
        await database.write(
            "UPDATE web_account SET must_change=0 WHERE username IN ('alpha','bravo','wallboard')"
        )
        web = create_web_app(lambda: {"radio": "up"}, database, auth, situation=service)
        alpha = TestClient(web)
        bravo = TestClient(web)
        wallboard = TestClient(web)
        for username, client in (("alpha", alpha), ("bravo", bravo), ("wallboard", wallboard)):
            response = client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": passwords[username]},
            )
            assert response.status_code == 200

        baseline = int(clock.now().timestamp())
        assert alpha.get("/api/v1/sitrep").json()["change_window"]["kind"] == "first_look"
        assert bravo.get("/api/v1/sitrep").json()["change_window"]["kind"] == "first_look"

        clock.advance(60)
        await database.write(
            "UPDATE incident SET title='Expanded fire perimeter',updated_at=? "
            "WHERE uid='local:one'",
            (int(clock.now().timestamp()),),
        )
        alpha_changed = alpha.get("/api/v1/sitrep").json()
        assert {(item["kind"], item["ref"]) for item in alpha_changed["changes"]} == {
            ("changed", "I1")
        }
        assert alpha_changed["change_window"]["label"].startswith("Since your last look at ")
        diverged_markers = await database.read(
            "SELECT account_id,last_seen_id FROM web_read_marker ORDER BY account_id"
        )
        assert len({int(row["last_seen_id"]) for row in diverged_markers}) == 2

        clock.advance(60)
        now = int(clock.now().timestamp())
        await database.write(
            "INSERT INTO alert(uid,severity,headline,source,channels,raised_by,raised_at,"
            "expires_at,ack_required) VALUES('alert:third','urgent','New evacuation zone',"
            "'operator','[0]','fixture',?,?,1)",
            (now, now + 3_600),
        )
        alpha_latest = alpha.get("/api/v1/sitrep").json()
        bravo_latest = bravo.get("/api/v1/sitrep").json()
        assert {(item["kind"], item["ref"]) for item in alpha_latest["changes"]} == {("new", "A3")}
        assert {(item["kind"], item["ref"]) for item in bravo_latest["changes"]} == {
            ("changed", "I1"),
            ("new", "A3"),
        }

        explicit_time = datetime.fromtimestamp(baseline, UTC).isoformat()
        explicit = alpha.get("/api/v1/sitrep", params={"since": explicit_time})
        assert explicit.status_code == 200
        assert explicit.json()["change_window"] == {
            "kind": "explicit",
            "since": baseline,
            "anchor_at": baseline,
            "complete": True,
            "label": "Since requested time 2026-08-28 12:00 UTC",
        }
        assert {(item["kind"], item["ref"]) for item in explicit.json()["changes"]} == {
            ("changed", "I1"),
            ("new", "A3"),
        }
        assert (
            alpha.get("/api/v1/sitrep", params={"since": "2026-08-28T12:00:00"}).status_code == 422
        )
        assert (
            alpha.get(
                "/api/v1/sitrep",
                params={"since": datetime.fromtimestamp(now + 60, UTC).isoformat()},
            ).status_code
            == 422
        )

        marker_rows = await database.read(
            "SELECT account_id,scope,last_seen_at,last_seen_id FROM web_read_marker "
            "ORDER BY account_id"
        )
        before_wallboard = [dict(row) for row in marker_rows]
        wallboard_response = wallboard.get("/api/v1/sitrep")
        assert wallboard_response.status_code == 200
        assert wallboard_response.json()["capability"] == "public"
        after_wallboard = [
            dict(row)
            for row in await database.read(
                "SELECT account_id,scope,last_seen_at,last_seen_id FROM web_read_marker "
                "ORDER BY account_id"
            )
        ]
        assert after_wallboard == before_wallboard
        assert {row["account_id"] for row in after_wallboard} == {
            int(accounts["alpha"]["id"]),
            int(accounts["bravo"]["id"]),
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_web_and_mesh_sitrep_work_without_ai(tmp_path) -> None:
    database = Database(tmp_path / "web.db")
    await database.open()
    clock = VirtualClock()
    service = SituationBriefingService(
        database,
        clock,
        lambda: {"radio": "down", "queues": {}, "inbound": {}},
    )
    client = TestClient(create_web_app(lambda: {"radio": "down"}, database, situation=service))
    try:
        response = client.get("/api/v1/sitrep?ai=true")
        assert response.status_code == 200
        assert response.json()["ai"]["outcome"] == "disabled"
        assert any(item["title"] == "Radio down" for item in response.json()["items"])
        assert "Deterministic facts remain authoritative" in client.get("/sitrep.html").text
        assert "Sitrep" in client.get("/nav.js").text
    finally:
        await database.close()

    app = OutpostApp(
        Config.model_validate(
            {
                "store": {"path": str(tmp_path / "mesh.db")},
                "modules": {"bbs": {"enabled": True}},
            }
        )
    )
    await app.database.open()
    try:
        sender = "!00000001"
        message = lambda packet, text, direct=True: InboundMessage(  # noqa: E731
            packet,
            sender,
            "!699c2f30" if direct else "^all",
            0,
            1,
            direct,
            text if direct else f"!{text}",
            None,
            datetime.now(UTC),
        )
        await app.router.dispatch(message(1, "NAME alex"))
        home_response = await app.router.dispatch(message(2, "SITREP"))
        home = render_response(home_response)
        assert home.startswith("OUTPOST / SITREP")
        assert all(
            label in home
            for label in (
                "Weather",
                "Incidents",
                "Welfare",
                "Community",
                "Delivery",
                "Network",
            )
        )
        assert home_response.max_parts == 1
        assert chunk_text(home) == [home]
        markers = await app.database.read(
            "SELECT r.scope,r.last_seen_id FROM read_marker r "
            "JOIN member m ON m.id=r.member_id WHERE m.mesh_id=?",
            (sender,),
        )
        assert len(markers) == 1 and markers[0]["scope"] == "sitrep:member"
        assert await app.database.read(
            "SELECT 1 FROM situation_snapshot WHERE id=? AND capability='member'",
            (markers[0]["last_seen_id"],),
        )
        network_response = await app.router.dispatch(message(3, "5"))
        assert "source ID@age" in render_response(network_response)
        assert network_response.max_parts == 2
        denied = render_response(await app.router.dispatch(message(4, "SITREP", direct=False)))
        assert "DM" in denied
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_viewer_web_sitrep_uses_public_capability(tmp_path) -> None:
    database, clock, service, _narrator = await seeded_service(tmp_path)
    auth = WebAuthService(database, 12)
    await auth.create_account(
        "wallboard",
        "Public display",
        "viewer",
        "wallboard-initial-42",
        "fixture",
    )
    client = TestClient(create_web_app(lambda: {"radio": "up"}, database, auth, situation=service))
    try:
        first = client.post(
            "/api/v1/auth/login",
            json={"username": "wallboard", "password": "wallboard-initial-42"},
        )
        changed = client.post(
            "/api/v1/auth/password",
            headers={"x-csrf-token": first.json()["csrf_token"]},
            json={"current_password": "", "new_password": "wallboard-permanent-42"},
        )
        assert changed.status_code == 200
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "wallboard", "password": "wallboard-permanent-42"},
        )
        assert login.status_code == 200
        response = client.get("/api/v1/sitrep")
        rendered = json.dumps(response.json())
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["capability"] == "public"
        assert "SECRET BOARD SUBJECT" not in rendered
        assert "@alex" not in rendered and "private medical note" not in rendered
        assert client.get("/api/v1/status").status_code == 403
    finally:
        await database.close()
