"""Synthetic node loss and reviewed identity continuity, using real store owners."""

import asyncio
import hashlib
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest

import outpost.fed.adoption as adoption_module
from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.fed.adoption import (
    AdoptionActor,
    AdoptionConflict,
    AdoptionDenied,
    FederationAdoptionService,
)
from outpost.transport.simulated import SimulatedRadioLink
from outpost.web.api import create_web_app
from tests.browser.test_incident_g6 import operator_client

pytestmark = pytest.mark.production_wiring
OLD, NEW, LOCAL = "!00000001", "!00000002", "!00000003"
BASE = f"/api/v1/federation/peers/{NEW}/adopt-origin"


@pytest.fixture
async def adoption_node(tmp_path):
    clock = VirtualClock()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "observer.db")},
            "modules": {"fed": {"enabled": True}, "watch": {"enabled": True}},
        }
    )
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock, node_id=LOCAL))
    await app.database.open()
    async with AsyncExitStack() as stack:
        stack.push_async_callback(app.database.close)
        stack.push_async_callback(app.ai_service.close)
        await app.radio.connect()
        app._radio_progress()
        client = await operator_client(app, stack)
        session = (await client.get("/api/v1/auth/session")).json()
        client.headers["x-csrf-token"] = session["csrf_token"]
        account = (await app.database.read("SELECT id FROM web_account"))[0]["id"]
        actor = AdoptionActor(
            account, hashlib.sha256(client.cookies.get("outpost_session").encode()).hexdigest()
        )
        for identity in (OLD, NEW):
            await app.federation.discover(identity, "Synthetic station", 1, {}, "radio")
        await app.database.write(
            "UPDATE fed_peer SET state='active',shared_secret=?,local_approved=1,"
            "remote_approved=1,tx_counter=21,rx_counter=40",
            (bytes(range(32)),),
        )
        await app.federation.set_state(OLD, "rejected")
        await app.database.write(
            "INSERT INTO fed_relay_origin_key(origin_node,public_key,fingerprint,state,"
            "first_seen_at) VALUES(?,?,?,'rejected',1)",
            (OLD, bytes(range(32)), hashlib.sha256(bytes(range(32))).hexdigest()),
        )
        thread = await app.database.write(
            "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at) "
            "VALUES(?,1,'Retained public history',?,1,1)",
            (OLD + ":local:1", OLD),
        )
        await app.database.write(
            "INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,created_at) "
            "VALUES(?,?,1,'synthetic',?,'Retained post',1)",
            (OLD + ":local:1", thread, OLD),
        )
        yield SimpleNamespace(
            app=app,
            db=app.database,
            service=app.federation_adoption,
            actor=actor,
            client=client,
            stack=stack,
        )


async def commit(node, preview=None):
    preview = preview or await node.service.review(node.actor, OLD, NEW)
    return await node.service.review(
        node.actor, OLD, NEW, expected_token=preview["review_token"], confirm_namespace=True
    )


async def test_review_commit_is_atomic_one_to_one_and_preserves_identity_counters(adoption_node):
    node = adoption_node
    before = [tuple(row) for row in await node.db.read("SELECT * FROM fed_peer ORDER BY id")]
    view = await node.service.review(node.actor, OLD, NEW)
    assert view["threads"] == view["posts"] == 1
    assert view["scope"] == "legacy_public_bbs_namespace_only"
    assert "fresh empty replacement" in view["warning"]
    assert not await node.db.read("SELECT * FROM fed_peer_successor")
    assert not await node.db.read("SELECT * FROM audit_log WHERE action='federation.origin_adopt'")
    assert (await commit(node, view))["ok"]
    assert before == [
        tuple(row) for row in await node.db.read("SELECT * FROM fed_peer ORDER BY id")
    ]
    assert len(await node.db.read("SELECT * FROM fed_peer_successor")) == 1
    audit = await node.db.read("SELECT * FROM audit_log WHERE action='federation.origin_adopt'")
    assert len(audit) == 1 and "legacy_public_bbs_namespace_only" in audit[0]["detail"]
    with pytest.raises(AdoptionConflict, match="already exists"):
        await commit(node, view)
    # Reappearance does not revive the old trust or reset the new key's counters.
    assert (await node.app.federation.discover(OLD, "Returned", 1, {}, "radio")).state == "rejected"
    with pytest.raises(ValueError, match="secret unavailable"):
        await node.app.federation.secret(OLD)
    assert not await node.app.federation.accept_counter(OLD, 100)
    assert not await node.app.federation.accept_counter(NEW, 40)
    assert await node.app.federation.accept_counter(NEW, 41)
    assert await node.app.federation.next_counter(NEW) == 22
    assert not node.app.radio.sent


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "bbs_disabled",
        "role",
        "account_disabled",
        "must_change",
        "session",
        "expired_session",
        "unpaired",
        "local_approval",
        "remote_approval",
        "secret",
        "short_secret",
        "predecessor_active",
        "predecessor_key",
        "pin",
        "history",
        "private_history",
    ],
)
async def test_commit_rechecks_every_current_authority_boundary(adoption_node, change):
    node = adoption_node
    view = await node.service.review(node.actor, OLD, NEW)
    changes = {
        "role": "UPDATE web_account SET role='viewer'",
        "account_disabled": "UPDATE web_account SET enabled=0",
        "must_change": "UPDATE web_account SET must_change=1",
        "session": "DELETE FROM web_session",
        "expired_session": "UPDATE web_session SET expires_at=0",
        "unpaired": "UPDATE fed_peer SET state='paused' WHERE mesh_id='!00000002'",
        "local_approval": "UPDATE fed_peer SET local_approved=0 WHERE mesh_id='!00000002'",
        "remote_approval": "UPDATE fed_peer SET remote_approved=0 WHERE mesh_id='!00000002'",
        "secret": "UPDATE fed_peer SET shared_secret=NULL WHERE mesh_id='!00000002'",
        "short_secret": "UPDATE fed_peer SET shared_secret=X'00' WHERE mesh_id='!00000002'",
        "predecessor_active": "UPDATE fed_peer SET state='active' WHERE mesh_id='!00000001'",
        "predecessor_key": "UPDATE fed_peer SET shared_secret=X'00' WHERE mesh_id='!00000001'",
        "pin": "UPDATE fed_relay_origin_key SET state='trusted'",
        "history": "DELETE FROM thread",
        "private_history": "UPDATE board SET min_read_trust='operator'",
    }
    if change in {"disabled", "bbs_disabled"}:
        getattr(node.app.config.modules, "fed" if change == "disabled" else "bbs").enabled = False
    else:
        await node.db.write(changes[change])
    with pytest.raises(AdoptionDenied):
        await commit(node, view)
    assert not await node.db.read("SELECT * FROM fed_peer_successor")
    assert not await node.db.read("SELECT * FROM audit_log WHERE action='federation.origin_adopt'")


@pytest.mark.parametrize(
    "change", ["key", "expiry", "restart", "label", "new_history", "token", "local_identity"]
)
async def test_preview_cannot_be_reused_in_a_changed_context(adoption_node, change):
    node = adoption_node
    view = await node.service.review(node.actor, OLD, NEW)
    label = None
    if change == "key":
        await node.db.write("UPDATE fed_peer SET shared_secret=? WHERE mesh_id=?", (b"k" * 32, NEW))
    elif change == "expiry":
        node.app.clock.advance(601)
    elif change == "restart":
        node.service = FederationAdoptionService(node.app.federation, lambda: True)
    elif change == "label":
        label = "Changed label"
    elif change == "token":
        view["review_token"] = "0" * 64
    elif change == "local_identity":
        node.app.federation.local_mesh_id = "!00000005"
    else:
        await node.db.write("UPDATE post SET hidden=1")
    with pytest.raises(AdoptionConflict, match="preview again"):
        await node.service.review(
            node.actor, OLD, NEW, label, expected_token=view["review_token"], confirm_namespace=True
        )
    assert not await node.db.read("SELECT * FROM fed_peer_successor")


async def test_invalid_identity_confirmation_and_legacy_aliases_fail_closed(adoption_node):
    node = adoption_node
    for old, new, label in (("!ABCDEF01", NEW, None), (OLD, "unknown", None), (OLD, NEW, "x" * 81)):
        with pytest.raises(ValueError):
            await node.service.review(node.actor, old, new, label)
    for old, new in ((OLD, OLD), (OLD, LOCAL), (LOCAL, NEW), (OLD, "!00000009")):
        with pytest.raises(AdoptionDenied):
            await node.service.review(node.actor, old, new)
    node.app.federation.local_mesh_id = ""
    with pytest.raises(AdoptionDenied, match="local station"):
        await node.service.review(node.actor, OLD, NEW)
    node.app.federation.local_mesh_id = LOCAL
    view = await node.service.review(node.actor, OLD, NEW)
    with pytest.raises(AdoptionDenied, match="confirmation"):
        await node.service.review(node.actor, OLD, NEW, expected_token=view["review_token"])
    await node.app.federation.forget(OLD)
    await node.db.write("DELETE FROM fed_relay_origin_key")
    await commit(node)
    # Imported legacy ambiguous rows must not arbitrarily alias a third origin.
    await node.db.write(
        "INSERT INTO fed_peer_successor VALUES('!00000004',"
        "(SELECT id FROM fed_peer WHERE mesh_id=?),NULL,1,'legacy')",
        (NEW,),
    )
    assert await node.app.federation_sync.canonical_remote_uid(NEW + ":local:1") == NEW + ":local:1"
    assert await node.app.federation_sync.canonical_remote_uid("local:1") == "local:1"
    assert (await node.app.federation.discover(OLD, "Returned", 1, {}, "radio")).state == "pending"
    with pytest.raises(ValueError, match="secret unavailable"):
        await node.app.federation.secret(OLD)
    assert await node.db.read("SELECT * FROM fed_peer_tombstone WHERE mesh_id=?", (OLD,))


async def test_legacy_chain_and_another_operator_review_cannot_be_adopted(adoption_node):
    node = adoption_node
    view = await node.service.review(node.actor, OLD, NEW)
    other = await operator_client(node.app, node.stack, username="second-operator")
    account = (await node.db.read("SELECT id FROM web_account WHERE username='second-operator'"))[
        0
    ]["id"]
    actor = AdoptionActor(
        account, hashlib.sha256(other.cookies.get("outpost_session").encode()).hexdigest()
    )
    with pytest.raises(AdoptionConflict, match="preview again"):
        await node.service.review(
            actor, OLD, NEW, expected_token=view["review_token"], confirm_namespace=True
        )
    await node.db.write(
        "INSERT INTO fed_peer_successor VALUES(?,(SELECT id FROM fed_peer WHERE mesh_id=?),"
        "NULL,1,'legacy')",
        (NEW, OLD),
    )
    with pytest.raises(AdoptionConflict, match="already exists"):
        await commit(node, view)
    await node.db.write(
        "INSERT INTO fed_peer_successor VALUES(?,(SELECT id FROM fed_peer WHERE mesh_id=?),"
        "NULL,1,'legacy')",
        (OLD, NEW),
    )
    assert await node.app.federation_sync.canonical_remote_uid(NEW + ":local:1") == NEW + ":local:1"


async def test_competing_commits_and_failed_audit_never_leave_partial_association(
    adoption_node, monkeypatch
):
    node = adoption_node
    view = await node.service.review(node.actor, OLD, NEW)
    original = adoption_module.write_audit

    async def failed_audit(*args, **kwargs):
        raise OSError("synthetic failed audit")

    monkeypatch.setattr(adoption_module, "write_audit", failed_audit)
    with pytest.raises(OSError):
        await commit(node, view)
    assert not await node.db.read("SELECT * FROM fed_peer_successor")
    monkeypatch.setattr(adoption_module, "write_audit", original)
    result = await asyncio.gather(commit(node, view), commit(node, view), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in result) == 1
    assert sum(isinstance(item, AdoptionConflict) for item in result) == 1
    assert len(await node.db.read("SELECT * FROM fed_peer_successor")) == 1
    assert (
        len(await node.db.read("SELECT * FROM audit_log WHERE action='federation.origin_adopt'"))
        == 1
    )


async def test_bbs_alias_never_suppresses_distinct_incident_or_alert(adoption_node):
    node = adoption_node
    await commit(node)
    await node.db.write("UPDATE board SET federated=1 WHERE slug='gen'")
    await node.db.write(
        "INSERT INTO incident(uid,title,type,severity,local_ref,reporter_label,origin_node,"
        "status,created_at,updated_at) "
        "VALUES(?,'Old hazard','hazard','caution',1,'synthetic','!00000001','open',1,100)",
        (OLD + ":local:1",),
    )
    await node.db.write(
        "INSERT INTO alert(uid,severity,headline,body,source,channels,raised_by,"
        "raised_at,expires_at) "
        "VALUES(?,'caution','Old alert','synthetic','operator','[]','synthetic',100,200)",
        (OLD + ":local:1",),
    )
    # A new producer with a reset/backward clock cannot inherit either old record.
    manifest = [
        {"stream": stream, "uid": NEW + ":local:1", "version": 1}
        for stream in ("incidents", "alerts", "board:gen")
    ]
    assert await node.app.federation_sync.missing(manifest) == [
        {"stream": item["stream"], "uid": item["uid"]} for item in manifest[:2]
    ]


async def test_real_http_requires_named_current_operator_csrf_and_complete_review(adoption_node):
    node = adoption_node
    anonymous = await node.stack.enter_async_context(
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=node.app.web), base_url="http://outpost.test"
        )
    )
    viewer = await operator_client(node.app, node.stack, role="viewer", username="observer")
    noauth = create_web_app(node.app.status, database=node.db, federation_adoption=node.service)
    bypass = await node.stack.enter_async_context(
        httpx.AsyncClient(transport=httpx.ASGITransport(app=noauth), base_url="http://outpost.test")
    )
    body = {"old_mesh_id": OLD}
    for client, status in ((anonymous, 401), (viewer, 403), (bypass, 403)):
        assert (await client.post(BASE + "/preview", json=body)).status_code == status
    assert (
        await node.client.post(BASE + "/preview", json=body, headers={"x-csrf-token": "wrong"})
    ).status_code == 403
    assert (await node.client.post(BASE, json=body)).status_code == 422
    response = await node.client.post(BASE + "/preview", json=body)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert (
        await node.client.post(BASE.replace(NEW, "invalid") + "/preview", json=body)
    ).status_code == 400
    commit_body = {
        **body,
        "review_token": response.json()["review_token"],
        "confirm_namespace": True,
    }
    for extra in ({"confirm_namespace": "true"}, {"actor": "administrator"}):
        assert (await node.client.post(BASE, json={**commit_body, **extra})).status_code == 422
    invalid = await node.client.post(BASE, json={**commit_body, "review_token": "0" * 64})
    assert invalid.status_code == 409 and invalid.headers["cache-control"] == "no-store"
    assert (await node.client.post(BASE, json=commit_body)).status_code == 200


@pytest.mark.parametrize("change", ["role", "session"])
async def test_revocation_between_http_auth_and_writer_is_not_stale_authority(
    adoption_node, monkeypatch, change
):
    node = adoption_node
    view = await node.service.review(node.actor, OLD, NEW)
    entered, release = asyncio.Event(), asyncio.Event()
    original = node.service.review

    async def pause(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(node.service, "review", pause)
    request = asyncio.create_task(
        node.client.post(
            BASE,
            json={
                "old_mesh_id": OLD,
                "review_token": view["review_token"],
                "confirm_namespace": True,
            },
        )
    )
    await entered.wait()
    await node.db.write(
        "DELETE FROM web_session" if change == "session" else "UPDATE web_account SET role='viewer'"
    )
    release.set()
    assert (await request).status_code == 403
    assert not await node.db.read("SELECT * FROM fed_peer_successor")
