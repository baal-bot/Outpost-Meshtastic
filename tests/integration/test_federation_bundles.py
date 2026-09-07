"""Two real, isolated stores: signed files, existing import owners, fault boundaries."""

import asyncio
import copy
import hashlib
import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import outpost.fed.bundles as bundles_module
from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.fed import bundle_format as fmt
from outpost.fed.bundles import BundleActor, BundleDenied
from outpost.fed.framing import MessageType
from outpost.fed.review import ReviewConflict
from outpost.store import Transaction
from outpost.store.backups import BackupService
from outpost.store.maintenance import MaintenanceService
from outpost.store.members import MemberRepo
from outpost.transport.simulated import SimulatedRadioLink
from tests.browser.test_incident_g6 import operator_client
from tests.integration.test_federation_incident_notes import source_note
from tests.integration.test_federation_item_failures import wire
from tests.integration.test_federation_revisions import drain

pytestmark = pytest.mark.production_wiring


@pytest.fixture
async def bundle_nodes(tmp_path):
    apps = []
    async with AsyncExitStack() as stack:
        for index in (1, 2):
            identity = f"!0000000{index}"
            clock = VirtualClock(epoch=datetime(2026, 1, index, tzinfo=UTC))
            config = Config.model_validate(
                {
                    "store": {"path": str(tmp_path / f"{index}.db")},
                    "modules": {"fed": {"enabled": True}, "watch": {"enabled": True}},
                    "airtime": {"dedupe_window_s": 0, "quiet_hours": {"classes": []}},
                }
            )
            app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock, node_id=identity))
            await app.database.open()
            stack.push_async_callback(app.database.close)
            stack.push_async_callback(app.ai_service.close)
            await app.radio.connect()
            app._radio_progress()
            await app.federation_relay.initialize()
            client = await operator_client(app, stack)
            session = (await client.get("/api/v1/auth/session")).json()
            client.headers["x-csrf-token"] = session["csrf_token"]
            account = (
                await app.database.read("SELECT id FROM web_account WHERE username='coordinator'")
            )[0]["id"]
            actor = BundleActor(
                account, hashlib.sha256(client.cookies.get("outpost_session").encode()).hexdigest()
            )
            await app.federation_bundles.commission(actor, identity)
            apps.append(
                SimpleNamespace(
                    app=app,
                    client=client,
                    actor=actor,
                    identity=identity,
                    service=app.federation_bundles,
                    stack=stack,
                )
            )
        for node, remote in ((apps[0], apps[1]), (apps[1], apps[0])):
            peer = await node.app.federation.discover(
                remote.identity,
                "Synthetic peer",
                1,
                {"reconciliation": 2, "incident_updates": 1},
                "radio",
            )
            await node.app.database.write(
                "UPDATE fed_peer SET state='active',shared_secret=?,boards='[\"gen\"]',"
                "sync_incidents=1,relay_alerts=1,local_approved=1,remote_approved=1,"
                "quota_items_per_hour=100,incident_lat=40,incident_lon=-79,"
                "incident_radius_km=100 WHERE id=?",
                (bytes(range(32)), peer.id),
            )
            await node.app.database.write("UPDATE board SET federated=1 WHERE slug='gen'")
            key = (await remote.app.database.read("SELECT public_key FROM fed_relay_identity"))[0][
                "public_key"
            ]
            await node.app.database.write(
                "INSERT INTO fed_relay_origin_key(origin_node,public_key,fingerprint,state,"
                "first_seen_at,reviewed_at) VALUES(?,?,?,'trusted',0,0)",
                (remote.identity, key, fmt.digest(key)),
            )
            node.peer = await node.app.federation.by_mesh_id(remote.identity)
            node.destination = remote.identity
        yield apps


async def incident(node, title="hazard synthetic blocked path"):
    result, _ = await node.app.incidents.create(
        title, None, force=True, operator_label="Synthetic reporter"
    )
    assert result is not None
    # These fixtures intentionally have no location; intake retains its raw
    # location description until cleared, which is correctly privacy-excluded.
    await node.app.database.write("UPDATE incident SET location_text=NULL WHERE id=?", (result.id,))
    return result


async def exported(node, *, stream="incidents", after=0, locations=False):
    options = {"public_labels": True, "precise_locations": locations}
    page = await node.service.export(node.actor, node.destination, stream, after, **options)
    assert page["items"], (
        page,
        [
            dict(row)
            for row in await node.app.database.read(
                "SELECT uid,lat,lon,location_text,reporter_label FROM incident"
            )
        ],
    )
    raw = await node.service.export(
        node.actor,
        node.destination,
        stream,
        after,
        **options,
        expected_token=page["review_token"],
        approve_public_content=True,
    )
    assert isinstance(raw, bytes)
    return raw, page


async def receive(node, raw):
    preview = await node.service.receive(node.actor, raw)
    return await node.service.receive(
        node.actor,
        raw,
        expected_token=preview["review_token"],
        approve_public_content=True,
        public_labels=True,
        precise_locations=True,
    )


async def signed(node, core):
    keys = (await node.app.database.read("SELECT private_key,public_key FROM fed_relay_identity"))[
        0
    ]
    return fmt.encode(core, bytes(keys["private_key"]), bytes(keys["public_key"]))


async def test_radio_off_restart_preview_atomic_import_and_later_revision_reconciliation(
    bundle_nodes,
):
    source, target = bundle_nodes
    original = await incident(source)
    raw, page = await exported(source)
    # Close the original stores and reconstruct complete application owners with
    # radios down and no observed ID, not just a warm service/cache reset.
    for node in bundle_nodes:
        old = node.app
        await old.ai_service.close()
        await old.database.close()
        node.app = OutpostApp(
            old.config, clock=old.clock, radio=SimulatedRadioLink(old.clock, node_id="")
        )
        await node.app.database.open()
        node.stack.push_async_callback(node.app.database.close)
        node.stack.push_async_callback(node.app.ai_service.close)
        node.service = node.app.federation_bundles
        await node.service.restore_identity()
        assert not node.app.radio.local_node_id
        assert node.app.federation.local_mesh_id == node.identity
    offline_raw, _ = await exported(source)
    assert fmt.decode(offline_raw).core["items"] == fmt.decode(raw).core["items"]
    before = await target.app.database.read("SELECT * FROM audit_log")
    preview = await target.service.receive(target.actor, raw)
    assert preview["effects"][0]["effect"] == "import"
    assert not await target.app.database.read("SELECT * FROM incident")
    assert not await target.app.database.read("SELECT * FROM fed_inbox_item")
    assert before == await target.app.database.read("SELECT * FROM audit_log")
    result = await receive(target, raw)
    assert result["imported"] == 1
    assert not source.app.radio.sent and not target.app.radio.sent
    assert not await target.app.database.read("SELECT * FROM outbound_work")
    assert not await target.app.database.read("SELECT * FROM incident_responsibility")
    with pytest.raises(ReviewConflict, match="already committed"):
        await target.service.receive(target.actor, raw)
    # Harmless outer whitespace cannot evade the replay identity.
    with pytest.raises(ReviewConflict, match="already committed"):
        await target.service.receive(target.actor, json.dumps(json.loads(raw), indent=2).encode())
    # Only now reconnect simulated radios for subsequent radio reconciliation.
    for node in bundle_nodes:
        node.app.radio._local_id = node.identity
        await node.app.radio.connect()
        node.app._radio_progress()
    # Unchanged radio page has nothing missing: exact payload/revision receipts match.
    manifest = await source.app.federation_sync.revisions.page(source.peer, {"cycle": "a" * 32})
    assert not await target.app.federation_sync.revisions.missing(
        target.peer, manifest["epoch"], manifest["items"]
    )
    await source.app.database.write(
        "UPDATE incident SET title='hazard revised path',updated_at=updated_at-3600 WHERE id=?",
        (original.id,),
    )
    queued = []

    async def control(peer, kind, value):
        queued.append((kind, copy.deepcopy(value)))
        return True

    target.app._queue_federation_control = control
    await target.app._federation_sync_once()
    await drain(source.app, source.peer, target.app, target.peer, queued)
    inbox = (await target.app.database.read("SELECT id FROM fed_inbox_item WHERE state='pending'"))[
        0
    ]
    await target.app.federation_sync.import_inbox(
        inbox["id"], "web:coordinator", int(target.app.clock.now().timestamp())
    )
    rows = await target.app.database.read("SELECT title FROM incident")
    assert len(rows) == 1 and rows[0]["title"] == "hazard revised path"
    assert len(await target.app.database.read("SELECT * FROM incident_origin")) == 1
    assert not await target.app.database.read("SELECT * FROM alert_ack")
    assert page["items"][0]["uid"].startswith(source.identity)


async def test_privacy_excludes_whole_records_and_never_exports_private_tables(
    bundle_nodes,
):
    source, target = bundle_nodes
    row = await incident(source)
    private = "SYNTHETIC PRIVATE WELFARE AND HANDOFF"
    member = await MemberRepo(source.app.database, source.app.clock).resolve("!00000003")
    await source.app.database.write(
        "INSERT INTO checkin(member_id,status,note,lat,lon,created_at) "
        "VALUES(?,'need_help',?,40.015,-79.015,0)",
        (member.id, private),
    )
    await source.app.database.write(
        "INSERT INTO mail(uid,from_label,to_label,body,created_at,expires_at) "
        "VALUES('private-fixture','Private sender','Private recipient',?,0,9999999999)",
        (private,),
    )
    owner = (
        await source.app.database.read(
            "SELECT id FROM incident_responsibility_target WHERE account_id=?",
            (source.actor.account_id,),
        )
    )[0]["id"]
    await source.app.database.write(
        "INSERT INTO incident_responsibility(incident_id,version,owner_id,next_action,updated_at) "
        "VALUES(?,1,?,?,0)",
        (row.id, owner, private),
    )
    await source.app.database.write(
        "UPDATE incident SET lat=40.01,lon=-79.01,location_text='Synthetic precise point' "
        "WHERE id=?",
        (row.id,),
    )
    default = await source.service.export(source.actor, target.identity, "incidents")
    labels = await source.service.export(
        source.actor, target.identity, "incidents", public_labels=True
    )
    assert not default["items"] and not labels["items"] and default["excluded"] == 1
    raw, page = await exported(source, locations=True)
    assert private not in json.dumps(fmt.decode(raw).core)
    item = page["items"][0]
    ordinary = await source.app.federation_sync.revisions.export(
        source.peer,
        {
            "cycle": "b" * 32,
            "epoch": item["epoch"],
            "scope": source.app.federation_sync.revisions.scope(source.peer),
            "items": [{"stream": "incidents", "uid": item["uid"], "revision": item["revision"]}],
        },
    )
    assert ordinary[0]["payload"] == item["payload"]
    assert item["payload"]["lat"] == 40.01
    for stream in (
        "mail",
        "welfare",
        "members",
        "accounts",
        "secrets",
        "responsibility",
        "board:private",
    ):
        with pytest.raises(ValueError):
            await source.service.export(
                source.actor, target.identity, stream, public_labels=True, precise_locations=True
            )
    view = await target.service.receive(target.actor, raw)
    for consent in (
        {},
        {"approve_public_content": True},
        {"approve_public_content": True, "public_labels": True},
    ):
        with pytest.raises(BundleDenied):
            await target.service.receive(
                target.actor, raw, expected_token=view["review_token"], **consent
            )
    result = await receive(target, raw)
    assert result["imported"] == 1
    assert not any(
        secret in raw
        for secret in (b"private_key", b"shared_secret", b"password_hash", b"session_hash")
    )


@pytest.mark.parametrize("boundary", ["quarantine", "domain", "audit", "receipt", "cancellation"])
async def test_entire_page_rolls_back_and_retries_after_import_fault(
    bundle_nodes, monkeypatch, boundary
):
    source, target = bundle_nodes
    await incident(source)
    await incident(source, "road synthetic second blockage")
    raw, _ = await exported(source)
    original = Transaction.write
    inserts = 0

    async def fail(tx, sql, params=()):
        nonlocal inserts
        if tx._database is target.app.database:
            if sql.startswith("INSERT INTO incident("):
                inserts += 1
            hit = (
                (boundary == "quarantine" and "INSERT INTO fed_inbox_item" in sql and inserts == 1)
                or (
                    boundary == "domain"
                    and sql.startswith("INSERT INTO incident(")
                    and inserts == 2
                )
                or (
                    boundary == "audit"
                    and "INSERT INTO audit_log" in sql
                    and params[2] == "federation.bundle.import"
                )
                or (
                    boundary in {"receipt", "cancellation"}
                    and "INSERT INTO fed_bundle_receipt" in sql
                )
            )
            if hit:
                if boundary == "cancellation":
                    raise asyncio.CancelledError()
                raise RuntimeError("synthetic writer fault")
        return await original(tx, sql, params)

    monkeypatch.setattr(Transaction, "write", fail)
    with pytest.raises(asyncio.CancelledError if boundary == "cancellation" else RuntimeError):
        await receive(target, raw)
    for table in (
        "incident",
        "incident_origin",
        "fed_inbox_item",
        "fed_revision_receipt",
        "fed_bundle_receipt",
    ):
        assert not await target.app.database.read(f"SELECT * FROM {table}")  # noqa: S608
    monkeypatch.setattr(Transaction, "write", original)
    assert (await receive(target, raw))["imported"] == 2


@pytest.mark.parametrize(
    "change",
    [
        "role",
        "session",
        "disabled",
        "module",
        "peer",
        "unpaired",
        "key",
        "revoked",
        "geography",
        "sync",
        "revision",
    ],
)
async def test_approval_rechecks_live_authority_policy_and_reviewed_version(bundle_nodes, change):
    source, target = bundle_nodes
    row = await incident(source)
    await source.app.database.write("UPDATE incident SET lat=40,lon=-79 WHERE id=?", (row.id,))
    raw, _ = await exported(source, locations=True)
    preview = await target.service.receive(target.actor, raw)
    changes = {
        "role": "UPDATE web_account SET role='viewer'",
        "session": "DELETE FROM web_session",
        "disabled": "UPDATE web_account SET enabled=0",
        "peer": "UPDATE fed_peer SET state='paused'",
        "unpaired": "UPDATE fed_peer SET remote_approved=0",
        "key": "UPDATE fed_relay_origin_key SET public_key=zeroblob(32)",
        "revoked": "UPDATE fed_relay_origin_key SET state='rejected'",
        "geography": "UPDATE fed_peer SET incident_lat=0,incident_lon=0",
        "sync": "UPDATE fed_peer SET sync_incidents=0",
    }
    if change in changes:
        await target.app.database.write(changes[change])
    elif change == "module":
        target.app.config.modules.fed.enabled = False
    else:
        await incident(target, "hazard concurrent local state")
    with pytest.raises(ValueError):
        await target.service.receive(
            target.actor,
            raw,
            expected_token=preview["review_token"],
            approve_public_content=True,
            public_labels=True,
            precise_locations=True,
        )
    assert not await target.app.database.read("SELECT * FROM fed_bundle_receipt")
    assert not await target.app.database.read("SELECT * FROM fed_revision_receipt")


async def test_export_review_changes_fail_closed_and_bounded_paging(bundle_nodes):
    source, target = bundle_nodes
    for index in range(10):
        await incident(source, f"hazard synthetic path {index}")
    raw, first = await exported(source)
    assert len(first["items"]) == 8 and not first["done"]
    _, second = await exported(source, after=first["next"])
    assert len(second["items"]) == 2 and second["done"]
    assert not set(item["uid"] for item in first["items"]) & set(
        item["uid"] for item in second["items"]
    )
    args = {
        "public_labels": True,
        "expected_token": first["review_token"],
        "approve_public_content": True,
    }
    with pytest.raises(BundleDenied):
        await source.service.export(
            source.actor,
            target.identity,
            "incidents",
            public_labels=True,
            expected_token=first["review_token"],
        )
    await source.app.database.write("UPDATE incident SET title='hazard revised preview' WHERE id=1")
    with pytest.raises(ReviewConflict):
        await source.service.export(source.actor, target.identity, "incidents", **args)
    for after in (-1, True, 2**63):
        with pytest.raises(ValueError):
            await source.service.export(
                source.actor, target.identity, "incidents", after, public_labels=True
            )
    assert (await receive(target, raw))["imported"] == 8


async def test_newer_revisions_and_prior_rejections_are_not_resurrected(bundle_nodes):
    source, target = bundle_nodes
    row = await incident(source)
    old, _ = await exported(source)
    await source.app.database.write(
        "UPDATE incident SET title='hazard new snapshot' WHERE id=?", (row.id,)
    )
    new, _ = await exported(source)
    assert (await receive(target, new))["imported"] == 1
    assert (await receive(target, old))["skipped"] == 1
    assert (await target.app.database.read("SELECT title FROM incident"))[0][
        "title"
    ] == "hazard new snapshot"
    # Re-sign the same producer version at a different advisory time: no duplicate action.
    core = copy.deepcopy(fmt.decode(new).core)
    core["created_at"] += 1
    assert (await receive(target, await signed(source, core)))["skipped"] == 1
    await incident(source, "road second snapshot")
    pending_raw, _ = await exported(source)
    items = fmt.decode(pending_raw).core["items"]
    for item in items:
        await target.app.federation_sync.quarantine(target.peer, item, 0)
    await target.app.database.write(
        "UPDATE fed_inbox_item SET state='rejected' WHERE state='pending'"
    )
    assert (await receive(target, pending_raw))["imported"] == 0
    assert len(await target.app.database.read("SELECT * FROM incident")) == 1


async def test_public_boards_notes_and_alerts_use_original_importers_without_actions(bundle_nodes):
    source, target = bundle_nodes
    member = await MemberRepo(source.app.database, source.app.clock).resolve("!00000003")
    await source.app.database.write(
        "UPDATE member SET trust='member',handle='Synthetic' WHERE id=?", (member.id,)
    )
    member = await MemberRepo(source.app.database, source.app.clock).resolve("!00000003")
    await source.app.bbs.create_thread("gen", "Public subject | Synthetic public body", member)
    board_raw, board_page = await exported(source, stream="board:gen")
    assert (await receive(target, board_raw))["imported"] == 1
    assert len(await target.app.database.read("SELECT * FROM post")) == 1
    assert (
        await target.service.export(target.actor, source.identity, "board:gen", public_labels=True)
    )["items"] == []
    row = await incident(source)
    _, note = await source_note(source.app, source.peer, row, body="Synthetic plain update")
    notes_raw, notes_page = await exported(source, stream="incident_updates")
    assert len(notes_page["items"]) == 1 and notes_page["items"][0]["uid"] == note["uid"]
    with pytest.raises(ValueError, match="parent incident"):
        await receive(target, notes_raw)
    assert not await target.app.database.read("SELECT * FROM incident_update")
    parent_raw, _ = await exported(source)
    await receive(target, parent_raw)
    assert (await receive(target, notes_raw))["imported"] == 1
    # Send the exact note through real authenticated application framing afterward.
    await wire(source.app, target.app, MessageType.ITEM, {"item": note})
    assert len(await target.app.database.read("SELECT * FROM incident_update")) == 1
    await source.app.database.write(
        "INSERT INTO alert(uid,severity,headline,source,channels,raised_by,raised_at) "
        "VALUES('synthetic-alert','caution','Synthetic alert','operator','[]',"
        "'Synthetic operator',0)"
    )
    raw, _ = await exported(source, stream="alerts")
    before = len(await target.app.database.read("SELECT * FROM outbound_work"))
    assert (await receive(target, raw))["imported"] == 1
    assert len(await target.app.database.read("SELECT * FROM outbound_work")) == before
    assert len(await target.app.database.read("SELECT * FROM alert")) == 1
    assert await target.app.alerts.advance_due() == 0
    assert not await target.app.database.read("SELECT * FROM alert_ack")
    assert not await target.app.database.read("SELECT * FROM incident_responsibility")
    # Receiver policy is checked again, including local board publication settings.
    document = fmt.decode(board_raw).core
    document["created_at"] += 1
    blocked = await signed(source, document)
    await target.app.database.write("UPDATE board SET federated=0 WHERE slug='gen'")
    with pytest.raises(BundleDenied, match="not currently federated"):
        await target.service.receive(target.actor, blocked)
    assert board_page["items"][0]["payload"]["thread_uid"]


async def test_pending_radio_item_is_reviewable_but_conflicting_lineage_and_digest_are_not(
    bundle_nodes,
):
    source, target = bundle_nodes
    await incident(source)
    raw, page = await exported(source)
    item = page["items"][0]
    await target.app.federation_sync.quarantine(target.peer, item, 0)
    preview = await target.service.receive(target.actor, raw)
    assert preview["effects"][0]["effect"] == "import_pending"
    assert (await receive(target, raw))["imported"] == 1
    for change in ("epoch", "payload"):
        document = copy.deepcopy(fmt.decode(raw).core)
        if change == "epoch":
            document["items"][0]["epoch"] = "b" * 32
        else:
            document["items"][0]["payload"]["title"] = "Conflicting same version"
        with pytest.raises(ReviewConflict):
            await target.service.receive(target.actor, await signed(source, document))
    assert len(await target.app.database.read("SELECT * FROM incident")) == 1


async def test_concurrent_acceptance_and_postcommit_cancellation_do_not_duplicate(
    bundle_nodes, monkeypatch
):
    source, target = bundle_nodes
    await incident(source)
    raw, _ = await exported(source)
    preview = await target.service.receive(target.actor, raw)

    async def commit():
        return await target.service.receive(
            target.actor,
            raw,
            expected_token=preview["review_token"],
            approve_public_content=True,
            public_labels=True,
        )

    results = await asyncio.gather(commit(), commit(), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in results) == 1
    assert sum(isinstance(value, ReviewConflict) for value in results) == 1
    await incident(source, "road new synthetic incident")
    raw, _ = await exported(source)
    old_audit = target.service._audit

    async def cancel_after_commit(tx, username, action, dest, detail):
        await old_audit(tx, username, action, dest, detail)
        if action == "import":
            owner = asyncio.current_task()

            def cancel():
                owner.cancel()

            tx.after_commit(cancel)

    monkeypatch.setattr(target.service, "_audit", cancel_after_commit)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(receive(target, raw))
    assert len(await target.app.database.read("SELECT * FROM incident")) == 2
    assert len(await target.app.database.read("SELECT * FROM fed_bundle_receipt")) == 2
    with pytest.raises(ReviewConflict, match="already committed"):
        await target.service.receive(target.actor, raw)


async def test_quota_capacity_unknown_pin_and_unavailable_identities_fail_closed(
    bundle_nodes, monkeypatch
):
    source, target = bundle_nodes
    await incident(source)
    await incident(source, "road second synthetic incident")
    raw, _ = await exported(source)
    await target.app.database.write("UPDATE fed_peer SET quota_items_per_hour=1")
    with pytest.raises(ValueError, match="quota"):
        await receive(target, raw)
    assert not await target.app.database.read("SELECT * FROM incident")
    await target.app.database.write("UPDATE fed_peer SET quota_items_per_hour=100")
    monkeypatch.setattr(bundles_module, "MAX_RECEIPTS", 1)
    await receive(target, raw)
    document = fmt.decode(raw).core
    document["created_at"] += 1
    again = await signed(source, document)
    with pytest.raises(BundleDenied, match="full"):
        await target.service.receive(target.actor, again)
    assert len(await target.app.database.read("SELECT * FROM fed_bundle_receipt")) == 1
    for state in ("observed", "rejected"):
        await target.app.database.write("UPDATE fed_relay_origin_key SET state=?", (state,))
        with pytest.raises(BundleDenied, match="not currently trusted"):
            await target.service.receive(target.actor, again)
    await target.app.database.write("DELETE FROM fed_relay_origin_key")
    with pytest.raises(BundleDenied):
        await target.service.receive(target.actor, again)
    assert not await target.app.database.read("SELECT * FROM fed_relay_origin_candidate")
    # Wrong destination, unknown peer, and missing commissioning never infer identity.
    document["destination"] = "!00000009"
    with pytest.raises(BundleDenied, match="different Outpost"):
        await target.service.receive(target.actor, await signed(source, document))
    await source.service.commission(source.actor, source.identity)
    await source.app.database.write("DELETE FROM fed_bundle_identity")
    with pytest.raises(BundleDenied, match="Commission"):
        await exported(source)
    for value in ("invalid", "!00000009"):
        with pytest.raises(ValueError):
            await source.service.commission(source.actor, value)
    source.app.federation.local_mesh_id = source.app.federation_sync.local_mesh_id = ""
    await source.service.restore_identity()
    assert not source.app.federation.local_mesh_id
    with pytest.raises(BundleDenied):
        await source.service.commission(source.actor, source.identity)


async def test_bundle_identity_and_ledger_survive_retention_and_peer_forgetting(bundle_nodes):
    source, target = bundle_nodes
    await incident(source)
    raw, _ = await exported(source)
    await receive(target, raw)
    target.app.clock.advance(400 * 86400)
    target.app.config.store.backup.enabled = False
    maintenance = MaintenanceService(
        target.app.database, BackupService(target.app.database), target.app.clock, target.app.config
    )
    assert await maintenance.audit_table_policies() == []
    result = await maintenance.run()
    assert result.failures == {}
    assert len(await target.app.database.read("SELECT * FROM fed_bundle_receipt")) == 1
    assert len(await target.app.database.read("SELECT * FROM fed_bundle_identity")) == 1
    ledger = (await target.app.database.read("SELECT * FROM fed_bundle_receipt"))[0]
    assert set(ledger.keys()) == {
        "digest",
        "origin",
        "fingerprint",
        "imported_at",
        "imported_by",
        "imported_count",
        "skipped_count",
    }
    await target.app.database.write("DELETE FROM fed_peer")
    assert len(await target.app.database.read("SELECT * FROM fed_bundle_receipt")) == 1
    # The 400-day maintenance step correctly expired the original web session.
    # Authenticate a fresh operator rather than weakening the authority check.
    target.client = await operator_client(target.app, target.stack, username="retention-reviewer")
    session = await target.app.web_auth.session(target.client.cookies.get("outpost_session"))
    target.actor = BundleActor(
        session.account_id,
        hashlib.sha256(target.client.cookies.get("outpost_session").encode()).hexdigest(),
    )
    assert (await target.service.status(target.actor))["commissioned_identity"] == target.identity
    with pytest.raises(BundleDenied, match="not been paired"):
        await target.service.receive(target.actor, raw)


async def test_clock_steps_restart_and_rotation_require_current_pin_not_signature_time(
    bundle_nodes,
):
    source, target = bundle_nodes
    await incident(source)
    raw, _ = await exported(source)
    source.app.clock.epoch -= timedelta(days=180)
    target.app.clock.epoch += timedelta(days=180)
    assert (await receive(target, raw))["imported"] == 1
    old_fingerprint = (await source.service.status(source.actor))["fingerprint"]
    await source.app.federation_relay.rotate_identity("web:coordinator")
    new_fingerprint = (await source.service.status(source.actor))["fingerprint"]
    assert old_fingerprint != new_fingerprint
    changed, _ = await exported(source)
    with pytest.raises(BundleDenied, match="not currently trusted"):
        await target.service.receive(target.actor, changed)
    # Explicit fixture pin replacement stands for the existing operator key-review workflow.
    key = fmt.decode(changed).public_key
    await target.app.database.write(
        "UPDATE fed_relay_origin_key SET public_key=?,fingerprint=?", (key, fmt.digest(key))
    )
    assert (await receive(target, changed))["imported"] == 0
    document = fmt.decode(raw).core
    document["created_at"] += 1
    from tests.unit.test_bundle_format import unvalidated

    # A valid but different untrusted synthetic key still cannot use an old timestamp.
    with pytest.raises(BundleDenied):
        await target.service.receive(target.actor, unvalidated(fmt.canonical(document)))
    assert len(await target.app.database.read("SELECT * FROM incident")) == 1
