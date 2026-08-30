import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from outpost.audit import write_audit
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.store import Database
from outpost.store.backups import BackupService
from outpost.store.maintenance import MaintenanceService
from outpost.store.members import MemberRepo
from outpost.transport.simulated import SimulatedRadioLink
from outpost.watch import AlertService, CheckinService, IncidentReportService, IncidentService
from outpost.web.api import create_web_app
from tests.support.application import production_governor

pytestmark = pytest.mark.production_wiring


@pytest.mark.asyncio
async def test_incident_report_merges_delivery_welfare_audit_and_privacy(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    clock, radio = VirtualClock(), SimulatedRadioLink()
    await radio.connect()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {3: {"name": "watch"}},
            "airtime": {"min_gap_s": 0},
            "watch": {
                "escalation": {
                    "urgent": {
                        "ack_threshold": 5,
                        "stages": [
                            {"after_minutes": 0, "notify": "responders", "channels": [3]},
                            {"after_minutes": 1, "notify": "responders", "channels": [3]},
                        ],
                    }
                }
            },
        }
    )
    members = MemberRepo(database, clock)
    reporter = await members.resolve("!00000001")
    responder = await members.resolve("!00000002")
    await database.write("UPDATE member SET handle='alice' WHERE id=?", (reporter.id,))
    await database.write(
        "UPDATE member SET handle='bravo',trust='responder' WHERE id=?", (responder.id,)
    )
    incidents = IncidentService(database, clock)
    incident, _ = await incidents.create(
        "fire <script>alert(1)</script> 40.44061 -79.99591", reporter
    )
    assert incident is not None
    await database.write(
        "INSERT INTO message_log(direction,peer_mesh_id,channel,portnum,is_direct,text,"
        "byte_len,outcome,transport,created_at) VALUES('in',?,3,1,1,?,4,'received','radio',?)",
        (reporter.mesh_id, "REPORT fire", incident.created_at),
    )
    event = await CheckinService(database, production_governor(database, clock), clock).open_event(
        "Fire response", "all", "operator"
    )
    checkins = CheckinService(database, production_governor(database, clock), clock)
    await checkins.checkin(reporter, "ok", "At assembly point")

    governor = production_governor(database, clock, link=radio, airtime=config.airtime)
    alerts = AlertService(database, governor, clock, config)
    alert = await alerts.raise_alert(
        "urgent", "Avoid Mill Road", "operator", incident_ref=incident.local_ref, channels=[3]
    )
    sent = await governor.tick()
    assert sent is not None
    await alerts.acknowledge(incident.local_ref, responder, "Responding")
    await incidents.operator_update(incident.id, "update", "=SUM(1,1)", actor="operator")
    await write_audit(
        database,
        actor_kind="web",
        actor_ref="operator",
        action="incident.resource_assigned",
        target=f"incident:{incident.id}",
        detail={
            "unit": "Engine 1",
            "location": {"latitude": 40.44061, "longitude": -79.99591},
        },
        created_at=int(clock.now().timestamp()),
    )
    await database.write("UPDATE member SET trust='guest' WHERE id=?", (responder.id,))
    clock.advance(61)
    assert await alerts.advance_due() == 0
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    clock.advance(300)
    assert await alerts.advance_due() == 1
    assert await governor.tick() is not None

    service = IncidentReportService(
        database,
        clock,
        config.store.retention,
        coarse_precision_m=config.security.coarse_precision_m,
    )
    report = await service.build(incident.id)

    assert report["summary"]["alert_count"] == 1
    assert report["summary"]["alert_stage_count"] == 3
    assert report["summary"]["zero_recipient_stages"] == 1
    assert report["summary"]["acknowledged_count"] == 1
    assert report["summary"]["welfare_checkin_count"] == 1
    assert report["summary"]["actual_airtime_ms"] > 0
    assert [item["timestamp"] for item in report["timeline"]] == sorted(
        item["timestamp"] for item in report["timeline"]
    )
    stage_rows = [item for item in report["timeline"] if item["category"] == "alert_stage"]
    assert [(item["addressed_count"], item["acknowledged_count"]) for item in stage_rows] == [
        (1, 1),
        (0, 0),
        (1, 0),
    ]
    encoded = json.dumps(report)
    assert "@alice" in encoded and "@bravo" in encoded
    assert reporter.mesh_id not in encoded and responder.mesh_id not in encoded
    assert "40.44061" not in encoded and "-79.99591" not in encoded
    assert report["incident"]["location"]["precision"] == "coarse"
    assert report["incident"]["location"]["lat"] != pytest.approx(40.44061)

    csv_value = service.csv_export(report)
    assert "event_id,timestamp,category" in csv_value
    assert "actual_toa_ms" in csv_value and "packet_id" in csv_value
    assert '"\'=SUM(1,1)"' in csv_value
    offline = service.offline_html(report)
    assert "Self-contained offline record" in offline
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in offline
    assert "<script>alert(1)</script>" not in offline
    assert "Audience:</b> @bravo" in offline

    account_id = await database.write(
        "INSERT INTO web_account(username,display_name,role,password_hash,must_change,"
        "created_at) VALUES('shift','Shift Operator','operator','hash',0,?)",
        (int(clock.now().timestamp()),),
    )
    first_handover = await service.handover(incident.id, account_id)
    assert first_handover["change_window"]["kind"] == "first_look"
    clock.advance(2)
    await incidents.operator_update(incident.id, "update", "Crew arrived", actor="shift")
    next_handover = await service.handover(incident.id, account_id)
    assert next_handover["change_window"]["kind"] == "viewer"
    assert any(item["detail"] == "Crew arrived" for item in next_handover["timeline"])
    assert next_handover["summary"]["alert_stage_count"] == sum(
        item["category"] == "alert_stage" for item in next_handover["timeline"]
    )
    assert next_handover["summary"]["actual_airtime_ms"] == sum(
        int(item.get("actual_toa_ms") or 0) for item in next_handover["timeline"]
    )

    client = TestClient(
        create_web_app(
            lambda: {"radio": "up"},
            database=database,
            incidents=incidents,
            incident_reports=service,
        )
    )
    assert client.get(f"/api/v1/incidents/{incident.id}/timeline").status_code == 200
    csv_response = client.get(f"/api/v1/incidents/{incident.id}/timeline.csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["x-outpost-data-classification"] == "coarse-operational-record"
    html_response = client.get(f"/api/v1/incidents/{incident.id}/offline.html")
    assert html_response.status_code == 200
    assert "attachment" in html_response.headers["content-disposition"]
    assert event.id > 0 and alert.id > 0
    await database.close()


@pytest.mark.asyncio
async def test_maintenance_protects_old_evidence_while_incident_is_retained(tmp_path) -> None:
    database = Database(tmp_path / "retention.db")
    await database.open()
    current = datetime(2026, 8, 30, tzinfo=UTC)
    old_clock = VirtualClock(epoch=current - timedelta(days=40))
    config = Config.model_validate(
        {
            "store": {
                "path": str(tmp_path / "retention.db"),
                "retention": {
                    "incident_history_days": 30,
                    "watch_history_days": 30,
                    "outbound_history_days": 30,
                    "message_log_days": 30,
                },
            },
            "channels": {3: {"name": "watch"}},
            "airtime": {"min_gap_s": 0},
        }
    )
    members = MemberRepo(database, old_clock)
    reporter = await members.resolve("!00000011")
    responder = await members.resolve("!00000012")
    await database.write("UPDATE member SET trust='responder' WHERE id=?", (responder.id,))
    incidents = IncidentService(database, old_clock)
    incident, _ = await incidents.create("fire at depot 40.4 -79.9", reporter)
    assert incident is not None
    await database.write(
        "INSERT INTO message_log(direction,peer_mesh_id,channel,portnum,is_direct,text,"
        "byte_len,outcome,transport,created_at) VALUES('in',?,3,1,1,'REPORT fire',4,"
        "'received','radio',?)",
        (reporter.mesh_id, incident.created_at),
    )
    radio = SimulatedRadioLink()
    await radio.connect()
    governor = production_governor(database, old_clock, link=radio, airtime=config.airtime)
    alert = await AlertService(database, governor, old_clock, config).raise_alert(
        "urgent", "Avoid depot", "operator", incident_ref=incident.local_ref, channels=[3]
    )
    assert await governor.tick() is not None
    checkins = CheckinService(database, governor, old_clock)
    event = await checkins.open_event("Depot response", "all", "operator")
    await checkins.checkin(reporter, "ok", "Clear")
    await checkins.close_event(event.id)

    current_clock = VirtualClock(epoch=current)
    await IncidentService(database, current_clock).operator_patch(
        incident.id,
        status="resolved",
        severity=None,
        resolution="Depot cleared",
        actor="operator",
    )
    maintenance = MaintenanceService(
        database, BackupService(database, config.store.backup), current_clock, config
    )
    preview = await maintenance.preview()
    eligible = {rule.key: rule.rows for rule in preview.rules}

    assert eligible["incidents"] == 0
    assert eligible["alerts"] == 0
    assert eligible["outbound_work"] == 0
    assert eligible["messages"] == 0
    assert eligible["checkins"] == 0
    assert eligible["watch_events"] == 0
    assert alert.id > 0
    await database.close()
